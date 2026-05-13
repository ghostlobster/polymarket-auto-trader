"""Smoke tests for the web profile UI."""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from copytrader.performance import reset_throttle_cache
from database import init_db
from models import TrackedTrader
from web.auth import require_auth
from web.server import build_app

_FAKE_USER = {
    "id": 1,
    "provider": "test",
    "provider_id": "1",
    "email": "test@example.com",
    "name": "Test",
    "avatar_url": None,
    "is_allowed": 1,
    "created_at": "2026-01-01",
    "last_login": "2026-01-01",
}


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await init_db(str(tmp_path / "web.db"))
    reset_throttle_cache()
    yield database
    await database.close()


def _make_client(db, **kwargs):
    """Build a TestClient with require_auth bypassed."""
    app = build_app(db)
    app.dependency_overrides[require_auth] = lambda: _FAKE_USER
    return TestClient(app, **kwargs)


@pytest.mark.asyncio
async def test_root_redirects_to_profiles(db):
    with _make_client(db, follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code in (302, 307)
        assert r.headers["location"].endswith("/profiles")


@pytest.mark.asyncio
async def test_profile_list_renders(db):
    await db.upsert_tracked_trader(
        TrackedTrader(
            wallet="0xabc",
            status="paper",
            preset="scaled_market",
            score=80.0,
            sample_size=50,
        )
    )
    with _make_client(db) as c:
        r = c.get("/profiles")
        assert r.status_code == 200
        assert "0xabc" in r.text
        assert "Tracked traders" in r.text


@pytest.mark.asyncio
async def test_profile_detail_404_when_unknown(db):
    with _make_client(db) as c:
        r = c.get("/profiles/0xnotfound")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_promote_blocked_when_preconditions_unmet(db):
    await db.upsert_tracked_trader(
        TrackedTrader(
            wallet="0xabc",
            status="paper",
            preset="scaled_market",
            score=80.0,
            sample_size=50,
        )
    )
    with _make_client(db, follow_redirects=False) as c:
        r = c.post("/profiles/0xabc/promote")
        # paper -> live needs ≥20 confirmed trades + positive PnL → blocked
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_promote_discovered_to_shadow_succeeds(db):
    await db.upsert_tracked_trader(
        TrackedTrader(
            wallet="0xabc",
            status="discovered",
            preset="scaled_market",
        )
    )
    with _make_client(db, follow_redirects=False) as c:
        r = c.post("/profiles/0xabc/promote")
        assert r.status_code == 303
    refreshed = await db.get_tracked_trader("0xabc")
    assert refreshed.status == "shadow"


@pytest.mark.asyncio
async def test_set_preset(db):
    await db.upsert_tracked_trader(
        TrackedTrader(
            wallet="0xabc",
            status="paper",
            preset="scaled_market",
        )
    )
    with _make_client(db, follow_redirects=False) as c:
        r = c.post("/profiles/0xabc/preset", data={"preset": "conservative"})
        assert r.status_code == 303
    refreshed = await db.get_tracked_trader("0xabc")
    assert refreshed.preset == "conservative"
