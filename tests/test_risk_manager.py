"""Tests for the deterministic risk manager + Kelly + guardrails."""

import pytest

from agents.risk_manager import RiskManagerAgent
from config import Settings
from models import PortfolioSnapshot, Position, Signal, SignalStrength
from risk import evaluate_guardrails, kelly_size


def make_signal(
    *,
    edge: float = 0.12,
    confidence: float = 0.75,
    market_price: float = 0.50,
    side: str = "YES",
    category: str = "Politics",
    cluster_id: str = "trump",
    resolves_at: str = "",
) -> Signal:
    return Signal(
        market_id="m1",
        question="Test market?",
        token_id="t1",
        side=side,
        strength=SignalStrength.BUY,
        estimated_probability=market_price + (edge if side == "YES" else -edge),
        market_price=market_price,
        edge=edge if side == "YES" else -edge,
        confidence=confidence,
        rationale="test",
        category=category,
        cluster_id=cluster_id,
        resolves_at=resolves_at,
    )


def make_portfolio(available=200.0, total=500.0, positions: list | None = None):
    return PortfolioSnapshot(
        total_usdc=total,
        available_usdc=available,
        open_positions=positions or [],
        realized_pnl=0.0,
        unrealized_pnl=0.0,
    )


# ------------------------------------------------------------------ #
#  kelly_size                                                        #
# ------------------------------------------------------------------ #


def test_kelly_positive_yes():
    res = kelly_size(edge=0.20, market_price=0.40, side="YES", bankroll=1000.0, kelly_fraction=0.25)
    # full kelly = 0.20 / 0.60 = 0.333...; fractional = 0.0833; size = 83.33
    assert res.kelly_full == pytest.approx(0.20 / 0.60, abs=1e-3)
    assert res.kelly_fraction_applied == pytest.approx(0.333 * 0.25, abs=1e-3)
    assert res.size_usdc == pytest.approx(0.333 * 0.25 * 1000, rel=0.01)


def test_kelly_negative_edge_for_yes_is_zero():
    res = kelly_size(
        edge=-0.10, market_price=0.50, side="YES", bankroll=1000.0, kelly_fraction=0.25
    )
    assert res.size_usdc == 0.0
    assert "no_positive_edge" in res.rationale


def test_kelly_no_side():
    # NO bet: edge is negative (we think YES is overpriced).
    res = kelly_size(edge=-0.20, market_price=0.60, side="NO", bankroll=1000.0, kelly_fraction=0.25)
    # full kelly = 0.20 / 0.60 = 0.333...
    assert res.kelly_full == pytest.approx(0.20 / 0.60, abs=1e-3)
    assert res.size_usdc > 0


def test_kelly_capped_at_one():
    res = kelly_size(
        edge=0.99, market_price=0.001, side="YES", bankroll=1000.0, kelly_fraction=0.25
    )
    # full kelly would be ~990; capped at 1.0
    assert res.kelly_full <= 1.0


# ------------------------------------------------------------------ #
#  guardrails                                                        #
# ------------------------------------------------------------------ #


def test_guardrails_approve_basic():
    settings = Settings(anthropic_api_key="test", max_position_usdc=50.0)
    signal = make_signal()
    portfolio = make_portfolio(available=500, total=500)
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=30.0,
        settings=settings,
    )
    assert report.approved is True
    assert report.size_usdc == pytest.approx(30.0)


def test_guardrails_caps_per_position():
    settings = Settings(anthropic_api_key="test", max_position_usdc=20.0)
    signal = make_signal()
    portfolio = make_portfolio(available=500, total=500)
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=100.0,
        settings=settings,
    )
    assert report.approved is True
    assert report.size_usdc == 20.0
    assert any("max_position_usdc_cap" in a for a in report.adjustments)


def test_guardrails_per_trade_frac_cap():
    settings = Settings(
        anthropic_api_key="test",
        max_position_usdc=1000.0,
        risk_per_trade_cap_frac=0.05,
    )
    signal = make_signal()
    portfolio = make_portfolio(available=1000, total=1000)
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=200.0,
        settings=settings,
    )
    assert report.size_usdc == pytest.approx(50.0)  # 5% of 1000


def test_guardrails_rejects_at_max_concurrent():
    settings = Settings(anthropic_api_key="test", max_concurrent_positions=2)
    signal = make_signal()
    positions = [
        Position(market_id=f"m{i}", token_id=f"t{i}", side="YES", size=10, avg_price=0.5)
        for i in range(2)
    ]
    portfolio = make_portfolio(available=500, total=500, positions=positions)
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=30.0,
        settings=settings,
    )
    assert report.approved is False
    assert "max_concurrent_positions" in report.reason


def test_guardrails_cluster_cap_shrinks():
    settings = Settings(
        anthropic_api_key="test",
        max_position_usdc=1000.0,
        risk_cluster_cap_frac=0.10,
    )
    signal = make_signal(cluster_id="elections")
    portfolio = make_portfolio(available=500, total=500)
    cluster_exposures = {"elections": 40.0}  # already 40 in cluster
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=50.0,
        settings=settings,
        cluster_exposures=cluster_exposures,
    )
    # cluster cap = 0.10*500 = 50; remaining = 50-40 = 10
    assert report.size_usdc == pytest.approx(10.0)


def test_guardrails_below_min_after_caps_rejects():
    settings = Settings(
        anthropic_api_key="test",
        risk_min_trade_usdc=10.0,
        risk_cluster_cap_frac=0.10,
    )
    signal = make_signal(cluster_id="x")
    portfolio = make_portfolio(available=100, total=100)
    cluster_exposures = {"x": 9.0}  # cap = 10, remaining = 1
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=50.0,
        settings=settings,
        cluster_exposures=cluster_exposures,
    )
    assert report.approved is False
    assert "size_below_min_after_caps" in report.reason


def test_guardrails_resolution_window_cap():
    settings = Settings(
        anthropic_api_key="test",
        risk_resolution_window_cap_frac=0.10,
    )
    signal = make_signal(resolves_at="2026-06-01T00:00:00Z")
    portfolio = make_portfolio(available=1000, total=1000)
    window_exposures = {"2026-06-01": 80.0}
    report = evaluate_guardrails(
        signal=signal,
        portfolio=portfolio,
        proposed_size_usdc=50.0,
        settings=settings,
        resolution_window_exposures=window_exposures,
    )
    # cap = 100, remaining = 20
    assert report.size_usdc == pytest.approx(20.0)


# ------------------------------------------------------------------ #
#  RiskManagerAgent.assess (integration)                             #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_risk_manager_assess_approves_valid_trade():
    settings = Settings(anthropic_api_key="test", max_position_usdc=50.0)
    agent = RiskManagerAgent(settings)
    signal = make_signal(edge=0.12, market_price=0.50)
    portfolio = make_portfolio(available=200, total=200)
    result = await agent.assess(signal, portfolio)
    assert result["approved"] is True
    assert result["size_usdc"] > 0
    assert result["size_usdc"] <= 50.0


@pytest.mark.asyncio
async def test_risk_manager_assess_rejects_at_max_concurrent():
    settings = Settings(anthropic_api_key="test", max_concurrent_positions=2)
    agent = RiskManagerAgent(settings)
    positions = [
        Position(market_id=f"m{i}", token_id=f"t{i}", side="YES", size=10, avg_price=0.5)
        for i in range(2)
    ]
    portfolio = make_portfolio(available=200, total=200, positions=positions)
    result = await agent.assess(make_signal(), portfolio)
    assert result["approved"] is False
    assert "max_concurrent_positions" in result["reason"]


@pytest.mark.asyncio
async def test_risk_manager_assess_honors_explicit_size():
    settings = Settings(anthropic_api_key="test", max_position_usdc=50.0)
    agent = RiskManagerAgent(settings)
    signal = make_signal(edge=0.0, market_price=0.50)
    portfolio = make_portfolio(available=500, total=500)
    # No edge → Kelly returns 0, but caller can override with explicit size.
    # With bankroll=500 the 10% per-trade cap is $50 so $25 sails through.
    result = await agent.assess(signal, portfolio, proposed_size_usdc=25.0)
    assert result["approved"] is True
    assert result["size_usdc"] == pytest.approx(25.0)
