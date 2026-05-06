"""
CopyAuditAgent — verifies that every leader trade we *expected* to copy
actually produced a corresponding order.

Two purposes:
  1. Pre-test confidence — proves the follow pipeline is wired up before
     anyone risks live capital. A wallet with high audit_miss_rate in shadow
     or paper is not eligible for promotion to the next stage.
  2. Live monitoring — catches silent failures (executor exceptions, rate
     limits, lost events) by surfacing alerts on the profile page.

Auto-demotes: if a trader's audit miss rate over the recent window exceeds
COPY_AUDIT_MISS_RATE_DEMOTE, the trader is moved one stage back
(live -> paper, paper -> shadow). Never auto-promotes.
"""
from datetime import datetime, timedelta

import structlog

from config import Settings
from copytrader.performance import recompute_for_wallet
from database import Database

log = structlog.get_logger(__name__)


_DEMOTE_TARGETS = {"live": "paper", "paper": "shadow", "shadow": "shadow"}


class CopyAuditAgent:
    def __init__(self, settings: Settings, db: Database, mark_to_market=None):
        self._settings = settings
        self._db = db
        self._mark_to_market = mark_to_market

    async def cycle(self) -> dict:
        traders = await self._db.get_active_tracked_traders()
        summary = {"traders": len(traders), "alerts": 0, "demoted": 0}
        cutoff = (
            datetime.utcnow() - timedelta(seconds=self._settings.copy_audit_window_secs)
        ).isoformat()

        for t in traders:
            unaudited = await self._db.get_unaudited_expected_trades(t.wallet, cutoff)
            for lt in unaudited:
                # Mark this miss with an alert so the profile page can show it.
                await self._db.record_audit_alert(
                    wallet=t.wallet,
                    leader_tx_hash=lt.tx_hash,
                    reason="expected_copy_no_order",
                )
                summary["alerts"] += 1

            # Recompute (forces a fresh row, ignoring throttle) so demotion logic
            # sees current numbers.
            perf = await recompute_for_wallet(
                self._db, t.wallet, mode=t.status if t.status != "discovered" else "shadow",
                throttle_secs=self._settings.copy_report_refresh_throttle_secs,
                mark_to_market=self._mark_to_market,
                force=True,
            )

            if (
                perf.audit_miss_rate >= self._settings.copy_audit_miss_rate_demote
                and perf.trades_observed >= 5
            ):
                target = _DEMOTE_TARGETS.get(t.status, t.status)
                if target != t.status:
                    await self._db.set_trader_status(t.wallet, target)
                    log.warning(
                        "Auto-demoted trader",
                        wallet=t.wallet, from_status=t.status, to_status=target,
                        miss_rate=perf.audit_miss_rate,
                    )
                    summary["demoted"] += 1

        return summary
