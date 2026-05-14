"""
CalibrationAuditor — closes the prediction→outcome loop.

For every unresolved signal in the database it (a) polls the Polymarket CLOB
for resolution status, (b) when resolved records the outcome in
`market_resolutions` and stamps the signal row with `was_correct`,
`realized_brier`, and `realized_log_loss`, then (c) rebuilds the
`calibration_buckets` table grouped by source × category × confidence-band.

The risk manager reads `calibration_buckets` to shrink edges on poorly-calibrated
buckets, so this auditor is the flywheel that makes every other improvement
compound. Pure async, no LLM in the path.
"""

import math
from datetime import datetime

import structlog

from config import Settings
from database import Database
from polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)


BANDS = [(i / 10.0, (i + 1) / 10.0) for i in range(10)]
EPS = 1e-6


def _band_for(p: float) -> tuple[float, float]:
    for lo, hi in BANDS:
        if lo <= p < hi:
            return lo, hi
    return 0.9, 1.0  # p == 1.0


def _bucket_key(source: str, category: str, lo: float, hi: float) -> str:
    return f"{source}|{category}|{lo:.2f}-{hi:.2f}"


class CalibrationAuditor:
    """
    Audit resolved markets and recompute calibration buckets.

    Typical use:
        auditor = CalibrationAuditor(settings, poly, db)
        await auditor.run_once()
    """

    name = "CalibrationAuditor"

    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db

    async def run_once(self) -> dict:
        """One audit pass — returns a summary dict for logging."""
        unresolved = await self._db.get_unresolved_signals(limit=500)
        summary = {"unresolved_signals": len(unresolved), "newly_resolved": 0, "buckets_updated": 0}

        resolutions: dict[str, dict | None] = {}
        for signal in unresolved:
            if signal.market_id in resolutions:
                resolution = resolutions[signal.market_id]
            else:
                resolution = await self._fetch_resolution(signal.market_id)
                resolutions[signal.market_id] = resolution

            if not resolution:
                continue

            outcome = resolution["resolved_outcome"]
            await self._db.upsert_market_resolution(
                condition_id=signal.market_id,
                resolved_outcome=outcome,
                resolved_at=resolution["resolved_at"],
                payout_token_id=resolution.get("payout_token_id", ""),
                source=resolution.get("source", "clob"),
            )
            if outcome not in ("YES", "NO"):
                continue

            actual = 1 if signal.side.upper() == outcome else 0
            p_our_side = (
                signal.estimated_probability
                if signal.side.upper() == "YES"
                else 1.0 - signal.estimated_probability
            )
            p_clipped = min(max(p_our_side, EPS), 1.0 - EPS)
            brier = (p_clipped - actual) ** 2
            log_loss = -math.log(p_clipped) if actual == 1 else -math.log(1.0 - p_clipped)

            await self._db.update_signal_resolution(
                signal_id=signal.id,
                resolved_outcome=outcome,
                resolved_at=resolution["resolved_at"],
                was_correct=actual,
                realized_brier=brier,
                realized_log_loss=log_loss,
            )
            summary["newly_resolved"] += 1

        # Rebuild calibration buckets from the (now extended) resolved-signals set.
        summary["buckets_updated"] = await self._rebuild_buckets()
        return summary

    # ------------------------------------------------------------------ #
    #  Resolution lookup                                                 #
    # ------------------------------------------------------------------ #

    async def _fetch_resolution(self, condition_id: str) -> dict | None:
        """
        Resolve a market through the CLOB. Returns None when the market is still
        active; returns {resolved_outcome, resolved_at, ...} when settled.

        Polymarket marks resolved markets with `closed: true` and a winning
        outcome on one of the token entries (`winner: true` or `payout: 1`).
        """
        already = await self._db.get_market_resolution(condition_id)
        if already:
            return already

        raw = await self._poly.get_market(condition_id)
        if not raw:
            return None
        if not (raw.get("closed") or raw.get("archived")):
            return None

        outcome = ""
        payout_token = ""
        for token in raw.get("tokens") or []:
            winner = bool(token.get("winner") or token.get("payout") == 1)
            if not winner:
                continue
            label = (token.get("outcome") or "").strip().lower()
            if label == "yes":
                outcome = "YES"
            elif label == "no":
                outcome = "NO"
            payout_token = token.get("token_id", "")
            break
        if not outcome:
            outcome = "INVALID"

        return {
            "resolved_outcome": outcome,
            "resolved_at": raw.get("end_date_iso") or datetime.utcnow().isoformat(),
            "payout_token_id": payout_token,
            "source": "clob",
        }

    # ------------------------------------------------------------------ #
    #  Bucket recompute                                                  #
    # ------------------------------------------------------------------ #

    async def _rebuild_buckets(self) -> int:
        signals = await self._db.get_resolved_signals(limit=10_000)
        # Group by (source, category, band)
        agg: dict[str, dict] = {}
        for s in signals:
            if s.was_correct is None or s.realized_brier is None:
                continue
            p = (
                s.estimated_probability
                if s.side.upper() == "YES"
                else 1.0 - s.estimated_probability
            )
            lo, hi = _band_for(p)
            cats = [s.category or "all"]
            if cats[0] != "all":
                cats.append("all")  # also feed an "all" rollup
            for cat in cats:
                key = _bucket_key(s.source or "thesis", cat, lo, hi)
                entry = agg.setdefault(
                    key,
                    {
                        "source": s.source or "thesis",
                        "category": cat,
                        "band_low": lo,
                        "band_high": hi,
                        "n": 0,
                        "pred_sum": 0.0,
                        "act_sum": 0.0,
                        "brier_sum": 0.0,
                        "log_loss_sum": 0.0,
                    },
                )
                entry["n"] += 1
                entry["pred_sum"] += p
                entry["act_sum"] += s.was_correct
                entry["brier_sum"] += s.realized_brier
                entry["log_loss_sum"] += s.realized_log_loss or 0.0

        for key, entry in agg.items():
            n = entry["n"] or 1
            await self._db.upsert_calibration_bucket(
                bucket_key=key,
                source=entry["source"],
                category=entry["category"],
                band_low=entry["band_low"],
                band_high=entry["band_high"],
                n=entry["n"],
                mean_predicted=entry["pred_sum"] / n,
                mean_actual=entry["act_sum"] / n,
                brier=entry["brier_sum"] / n,
                log_loss=entry["log_loss_sum"] / n,
            )
        return len(agg)
