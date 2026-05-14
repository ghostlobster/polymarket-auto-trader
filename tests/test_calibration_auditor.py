"""Tests for the calibration auditor and bucket recomputation."""

import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from agents.calibration_auditor import CalibrationAuditor
from config import Settings
from database import init_db
from models import Signal, SignalStrength


@pytest.mark.asyncio
async def test_audit_resolves_and_scores_signal():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = await init_db(tmp.name)
        signal = Signal(
            id="s1",
            market_id="cond1",
            question="q?",
            token_id="t1",
            side="YES",
            strength=SignalStrength.BUY,
            estimated_probability=0.75,
            market_price=0.5,
            edge=0.25,
            confidence=0.8,
            rationale="x",
            category="Politics",
        )
        await db.save_signal(signal)

        # Fake a Polymarket client that returns a resolved market.
        poly = AsyncMock()
        poly.get_market = AsyncMock(
            return_value={
                "closed": True,
                "end_date_iso": "2026-05-01T00:00:00Z",
                "tokens": [
                    {"outcome": "Yes", "winner": True, "token_id": "t1"},
                    {"outcome": "No", "winner": False, "token_id": "t2"},
                ],
            }
        )

        settings = Settings(anthropic_api_key="test")
        auditor = CalibrationAuditor(settings, poly, db)
        summary = await auditor.run_once()
        assert summary["newly_resolved"] == 1
        assert summary["buckets_updated"] >= 1

        # Signal must be marked correct, with realized_brier ≈ (0.75 - 1)^2 = 0.0625
        resolved = await db.get_resolved_signals()
        assert len(resolved) == 1
        s = resolved[0]
        assert s.was_correct == 1
        assert s.realized_brier == pytest.approx(0.0625, abs=1e-4)

        # Calibration buckets exist
        buckets = await db.get_calibration_buckets()
        assert len(buckets) >= 1

        await db.close()
    finally:
        os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_audit_skips_unresolved_market():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db = await init_db(tmp.name)
        signal = Signal(
            id="s2",
            market_id="cond2",
            question="q?",
            token_id="t1",
            side="NO",
            strength=SignalStrength.BUY,
            estimated_probability=0.30,
            market_price=0.5,
            edge=-0.20,
            confidence=0.8,
            rationale="x",
            category="Politics",
        )
        await db.save_signal(signal)

        poly = AsyncMock()
        poly.get_market = AsyncMock(return_value={"closed": False, "tokens": []})

        auditor = CalibrationAuditor(Settings(anthropic_api_key="test"), poly, db)
        summary = await auditor.run_once()
        assert summary["newly_resolved"] == 0

        unresolved = await db.get_unresolved_signals()
        assert len(unresolved) == 1
        await db.close()
    finally:
        os.unlink(tmp.name)
