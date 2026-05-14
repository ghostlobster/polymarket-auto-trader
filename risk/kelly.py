"""
Deterministic Kelly Criterion sizing for binary prediction markets.

A Polymarket position is a binary bet: paying `p` (market price, in [0,1]) to win
$1 if the outcome resolves your way. The Kelly fraction of bankroll for such a
bet is `edge / (1 - p)` for YES (or `(-edge) / p` for NO).

This module returns a `KellyResult` with the raw Kelly fraction, the fractional
Kelly applied (per `Settings.kelly_fraction`), and the resulting USDC notional —
all clamped to non-negative values. Pure Python; no I/O, no LLM.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class KellyResult:
    kelly_full: float  # fraction of bankroll under full Kelly
    kelly_fraction_applied: float  # fraction after applying settings.kelly_fraction
    size_usdc: float  # final notional before guardrails
    rationale: str


def kelly_size(
    *,
    edge: float,
    market_price: float,
    side: str,
    bankroll: float,
    kelly_fraction: float,
) -> KellyResult:
    """
    Compute fractional Kelly size for a binary bet on a Polymarket outcome.

    Inputs:
      edge          — estimated_probability − market_price (signed). For a YES bet
                      we want edge > 0; for NO bet we want edge < 0.
      market_price  — current market price in [0, 1] for the YES token.
      side          — "YES" or "NO".
      bankroll      — available USDC to allocate.
      kelly_fraction— fractional-Kelly multiplier from settings (e.g. 0.25).

    The full Kelly fraction is unbounded above but capped at 1.0 to avoid pathological
    sizes from near-zero opposing prices.
    """
    side = (side or "YES").upper()
    p = max(0.001, min(0.999, float(market_price)))
    e = float(edge)

    if side == "YES":
        if e <= 0:
            return KellyResult(0.0, 0.0, 0.0, "no_positive_edge_for_yes")
        kelly_full = e / (1.0 - p)
    elif side == "NO":
        if e >= 0:
            return KellyResult(0.0, 0.0, 0.0, "no_negative_edge_for_no")
        kelly_full = (-e) / p
    else:
        return KellyResult(0.0, 0.0, 0.0, f"unknown_side:{side}")

    kelly_full = max(0.0, min(1.0, kelly_full))
    applied = kelly_full * max(0.0, float(kelly_fraction))
    size = max(0.0, applied * max(0.0, float(bankroll)))
    return KellyResult(
        kelly_full=round(kelly_full, 6),
        kelly_fraction_applied=round(applied, 6),
        size_usdc=round(size, 4),
        rationale=f"kelly_full={kelly_full:.4f} × frac={kelly_fraction:.2f} × bankroll={bankroll:.2f}",
    )
