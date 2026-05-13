"""
RiskManagerAgent: deterministic Kelly sizing + portfolio guardrails.

The previous implementation delegated Kelly math to an LLM, which made limit
breaches a hallucination risk. This version performs the math algorithmically;
the LLM is no longer in the critical path and is invoked only for an optional
human-readable explanation when `explain=True`.
"""

import structlog

from config import Settings
from database import Database
from models import PortfolioSnapshot, Signal
from risk import evaluate_guardrails, kelly_size

log = structlog.get_logger(__name__)


class RiskManagerAgent:
    """
    Deterministic risk manager.

    `assess()` returns a dict shaped like the legacy LLM output for backwards
    compatibility with the orchestrator, plus extra fields under "details".
    """

    def __init__(self, settings: Settings, db: Database | None = None):
        self._settings = settings
        self._db = db
        self.name = "RiskManager"

    async def assess(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        proposed_size_usdc: float | None = None,
    ) -> dict:
        # 1) Compute size — either Kelly (default) or honor an externally-decided size
        #    (e.g. copy-trader passes the preset-sized notional, no Kelly involved).
        if proposed_size_usdc is None:
            kelly = kelly_size(
                edge=signal.edge,
                market_price=signal.market_price,
                side=signal.side,
                bankroll=portfolio.total_usdc or portfolio.available_usdc or 0.0,
                kelly_fraction=self._settings.kelly_fraction,
            )
            raw_size = kelly.size_usdc
            kelly_full = kelly.kelly_full
            kelly_applied = kelly.kelly_fraction_applied
        else:
            raw_size = max(0.0, float(proposed_size_usdc))
            kelly_full = 0.0
            kelly_applied = 0.0

        # 2) Apply calibration shrinkage (if calibration data is available)
        shrink_factor = await self._calibration_shrinkage(signal)
        shrunk_size = raw_size * shrink_factor

        # 3) Compute exposures from open positions
        cluster_exposures, category_exposures, window_exposures = self._exposure_buckets(
            portfolio, signal
        )

        # 4) Apply guardrails
        report = evaluate_guardrails(
            signal=signal,
            portfolio=portfolio,
            proposed_size_usdc=shrunk_size,
            settings=self._settings,
            cluster_exposures=cluster_exposures,
            category_exposures=category_exposures,
            resolution_window_exposures=window_exposures,
        )

        signal.applied_shrinkage = shrink_factor

        decision = {
            "approved": report.approved,
            "size_usdc": round(report.size_usdc, 4),
            "reason": report.reason,
            "kelly_pct": kelly_applied,
            "details": {
                "kelly_full": kelly_full,
                "raw_kelly_size_usdc": raw_size,
                "shrinkage": shrink_factor,
                "post_shrinkage_size_usdc": round(shrunk_size, 4),
                "raw_proposed_size_usdc": report.raw_size_usdc,
                "adjustments": report.adjustments,
                "cluster_id": signal.cluster_id or signal.category or "uncategorized",
                "cluster_exposure_before": report.cluster_exposure_before,
                "category_exposure_before": report.category_exposure_before,
                "resolution_window_exposure_before": report.resolution_window_exposure_before,
            },
        }
        log.info(
            "Risk decision",
            approved=decision["approved"],
            size_usdc=decision["size_usdc"],
            reason=decision["reason"],
            shrinkage=shrink_factor,
            kelly_pct=kelly_applied,
        )
        return decision

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    async def _calibration_shrinkage(self, signal: Signal) -> float:
        """
        Look up the matching calibration bucket for this signal and return a
        shrinkage factor in [shrinkage_floor, 1.0]. The factor is
        `mean_actual / mean_predicted` for the bucket — i.e., scale our edge by
        how well that confidence band has historically performed.
        """
        if self._db is None:
            return 1.0
        try:
            buckets = await self._db.get_calibration_buckets(source=signal.source)
        except Exception:
            return 1.0
        if not buckets:
            return 1.0

        # Probability our side wins (per our model): if YES, estimated_probability;
        # if NO, 1 - estimated_probability.
        p = (
            signal.estimated_probability
            if signal.side.upper() == "YES"
            else 1.0 - signal.estimated_probability
        )
        match = None
        for b in buckets:
            if b["category"] and signal.category and b["category"] != signal.category:
                continue
            if float(b["band_low"]) <= p < float(b["band_high"]):
                match = b
                break
        if match is None:
            return 1.0
        if int(match.get("n", 0)) < self._settings.calibration_min_resolutions_for_shrinkage:
            return 1.0
        mean_predicted = float(match["mean_predicted"]) or 1e-6
        mean_actual = float(match["mean_actual"])
        factor = mean_actual / mean_predicted
        # Only shrink (factor < 1); never amplify edges from optimistic buckets.
        factor = min(1.0, max(self._settings.risk_shrinkage_floor, factor))
        return round(factor, 4)

    def _exposure_buckets(
        self, portfolio: PortfolioSnapshot, signal: Signal
    ) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
        """
        Build (cluster, category, resolution-day) exposure dicts from open positions.
        Without per-position cluster metadata we conservatively bucket everything
        under the signal's own cluster/category — i.e. the cap considers ALL open
        positions as potentially correlated when we lack information to prove
        otherwise.
        """
        sig_cluster = (signal.cluster_id or signal.category or "uncategorized").lower()
        sig_category = (signal.category or "other").lower()
        cluster: dict[str, float] = {sig_cluster: 0.0}
        category: dict[str, float] = {sig_category: 0.0}
        window: dict[str, float] = {}
        for pos in portfolio.open_positions:
            notional = float(pos.size) * float(pos.avg_price)
            cluster[sig_cluster] = cluster.get(sig_cluster, 0.0) + notional
            category[sig_category] = category.get(sig_category, 0.0) + notional
        return cluster, category, window
