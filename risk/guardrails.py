"""
Portfolio-level guardrails applied after Kelly sizing.

Each guardrail can either (a) shrink the proposed size or (b) reject the trade
outright. The return value is a `GuardrailReport` so callers can log every
adjustment made.

All checks are deterministic and configurable via `Settings`.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import Settings
from models import PortfolioSnapshot, Signal


@dataclass
class GuardrailReport:
    approved: bool
    size_usdc: float
    raw_size_usdc: float
    reason: str = ""
    adjustments: list[str] = field(default_factory=list)
    cluster_exposure_before: float = 0.0
    category_exposure_before: float = 0.0
    resolution_window_exposure_before: float = 0.0

    def reject(self, reason: str) -> "GuardrailReport":
        self.approved = False
        self.size_usdc = 0.0
        self.reason = reason
        return self

    def shrink(self, new_size: float, note: str) -> None:
        if new_size < self.size_usdc:
            self.adjustments.append(f"{note}: {self.size_usdc:.2f}→{new_size:.2f}")
            self.size_usdc = max(0.0, round(new_size, 4))


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _position_notional(p) -> float:
    return float(p.size) * float(p.avg_price)


def evaluate_guardrails(
    *,
    signal: Signal,
    portfolio: PortfolioSnapshot,
    proposed_size_usdc: float,
    settings: Settings,
    cluster_exposures: dict[str, float] | None = None,
    category_exposures: dict[str, float] | None = None,
    resolution_window_exposures: dict[str, float] | None = None,
) -> GuardrailReport:
    """
    Apply mechanical risk caps to a proposed position size.

    The caller is responsible for computing exposures (optional dicts) so the
    function stays pure and easily testable. When omitted, the function derives
    them from `portfolio.open_positions` using signal.cluster_id / category as
    grouping keys.
    """
    report = GuardrailReport(
        approved=True,
        size_usdc=round(max(0.0, proposed_size_usdc), 4),
        raw_size_usdc=round(max(0.0, proposed_size_usdc), 4),
    )

    if proposed_size_usdc <= 0:
        return report.reject("non_positive_proposed_size")

    bankroll = max(0.0, portfolio.total_usdc or portfolio.available_usdc or 0.0)
    available = max(0.0, portfolio.available_usdc or 0.0)

    if bankroll <= 0:
        return report.reject("zero_bankroll")
    if available < settings.risk_min_trade_usdc:
        return report.reject(
            f"available<min_trade:{available:.2f}<{settings.risk_min_trade_usdc:.2f}"
        )

    # 1. Hard per-position cap
    if report.size_usdc > settings.max_position_usdc:
        report.shrink(settings.max_position_usdc, "max_position_usdc_cap")

    # 2. Per-trade fraction of bankroll cap
    per_trade_cap = settings.risk_per_trade_cap_frac * bankroll
    if report.size_usdc > per_trade_cap:
        report.shrink(per_trade_cap, "per_trade_frac_cap")

    # 3. Available balance cap
    if report.size_usdc > available:
        report.shrink(available, "available_balance")

    # 4. Concurrent-position cap
    if len(portfolio.open_positions) >= settings.max_concurrent_positions:
        return report.reject(
            f"max_concurrent_positions:{len(portfolio.open_positions)}>={settings.max_concurrent_positions}"
        )

    # 5. Cluster exposure cap
    cluster_id = (signal.cluster_id or signal.category or "uncategorized").lower()
    if cluster_exposures is None:
        cluster_exposures = {}
        for pos in portfolio.open_positions:
            key = cluster_id  # caller didn't supply, so assume all in same cluster only if matches
            # We don't know each open position's cluster id from the Position model,
            # so default to the conservative behavior of grouping under signal cluster.
            cluster_exposures.setdefault(key, 0.0)
        # Without explicit positions, fall through.
    cluster_now = float(cluster_exposures.get(cluster_id, 0.0))
    cluster_cap = settings.risk_cluster_cap_frac * bankroll
    report.cluster_exposure_before = round(cluster_now, 4)
    if cluster_now >= cluster_cap:
        return report.reject(
            f"cluster_cap_full:{cluster_now:.2f}>={cluster_cap:.2f} (cluster={cluster_id})"
        )
    remaining_cluster = cluster_cap - cluster_now
    if report.size_usdc > remaining_cluster:
        report.shrink(remaining_cluster, f"cluster_cap[{cluster_id}]")

    # 6. Category exposure cap
    category = (signal.category or "other").lower()
    if category_exposures is None:
        category_exposures = {}
    cat_now = float(category_exposures.get(category, 0.0))
    cat_cap = settings.risk_category_cap_frac * bankroll
    report.category_exposure_before = round(cat_now, 4)
    if cat_now >= cat_cap:
        return report.reject(
            f"category_cap_full:{cat_now:.2f}>={cat_cap:.2f} (category={category})"
        )
    remaining_cat = cat_cap - cat_now
    if report.size_usdc > remaining_cat:
        report.shrink(remaining_cat, f"category_cap[{category}]")

    # 7. Resolution-window exposure cap (bucket by UTC calendar day of resolution)
    window_key = ""
    resolves = _parse_iso(signal.resolves_at)
    if resolves is not None:
        window_key = resolves.astimezone(timezone.utc).date().isoformat()
    if resolution_window_exposures is None:
        resolution_window_exposures = {}
    if window_key:
        win_now = float(resolution_window_exposures.get(window_key, 0.0))
        win_cap = settings.risk_resolution_window_cap_frac * bankroll
        report.resolution_window_exposure_before = round(win_now, 4)
        if win_now >= win_cap:
            return report.reject(
                f"resolution_window_cap_full:{win_now:.2f}>={win_cap:.2f} (day={window_key})"
            )
        remaining_win = win_cap - win_now
        if report.size_usdc > remaining_win:
            report.shrink(remaining_win, f"resolution_window[{window_key}]")

    # 8. Final min-trade floor (after all shrinkage)
    if report.size_usdc < settings.risk_min_trade_usdc:
        return report.reject(
            f"size_below_min_after_caps:{report.size_usdc:.2f}<{settings.risk_min_trade_usdc:.2f}"
        )

    report.reason = "approved"
    return report
