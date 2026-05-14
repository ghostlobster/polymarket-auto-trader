"""
FastAPI app exposing per-profile review pages.

Endpoints:
  GET  /                          → redirect to /profiles
  GET  /login                     → OAuth login page
  GET  /auth/google               → begin Google OAuth flow
  GET  /auth/google/callback      → Google OAuth callback
  GET  /auth/github               → begin GitHub OAuth flow
  GET  /auth/github/callback      → GitHub OAuth callback
  GET  /logout                    → clear session and redirect to /login
  GET  /profiles                  → list of tracked traders (refreshes rollups)
  GET  /profiles/{wallet}         → detailed view (refreshes rollups)
  POST /profiles/{wallet}/promote → status one step forward, with gating
  POST /profiles/{wallet}/preset  → change strategy preset
  POST /profiles/{wallet}/disable → set status='disabled'

The on-access refresh is what the user asked for: every page load triggers a
recompute of `copy_performance` (subject to the 30s throttle in
`copytrader.performance`), so the report is always live without a separate cron.
"""

from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import Settings
from copytrader.performance import recompute_for_wallet
from copytrader.strategies import PRESETS
from database import Database
from models import TRADER_STATUSES
from web.auth import NeedsLogin, configure_oauth, make_auth_router, require_auth

log = structlog.get_logger(__name__)


_TEMPLATES_DIR = Path(__file__).parent / "templates"


# Promotion ladder + preconditions
def _can_promote(trader, perf, settings) -> tuple[bool, str]:
    if trader.status == "discovered":
        return True, ""  # discovered → shadow is always allowed
    if trader.status == "shadow":
        if perf.trades_observed < 5:
            return False, "shadow→paper: need ≥5 observed trades"
        if perf.audit_miss_rate >= settings.copy_audit_miss_rate_demote:
            return False, f"shadow→paper: audit miss rate {perf.audit_miss_rate:.1%} too high"
        return True, ""
    if trader.status == "paper":
        if perf.trades_copied < settings.copy_min_confirmed_paper_trades:
            return (
                False,
                f"paper→live: need ≥{settings.copy_min_confirmed_paper_trades} confirmed paper trades",
            )
        if perf.realized_pnl + perf.unrealized_pnl <= 0:
            return False, "paper→live: paper PnL must be positive"
        if perf.audit_miss_rate >= settings.copy_audit_miss_rate_demote:
            return False, f"paper→live: audit miss rate {perf.audit_miss_rate:.1%} too high"
        return True, ""
    return False, f"no further promotion from status={trader.status}"


def _next_status(current: str) -> str:
    return {"discovered": "shadow", "shadow": "paper", "paper": "live"}.get(current, current)


def build_app(db: Database, copy_agent=None, mark_to_market=None) -> FastAPI:
    """
    Construct the FastAPI app. `copy_agent` and `mark_to_market` are optional —
    if absent, the page still renders but skips MTM (useful in tests).
    """
    settings = Settings()
    app = FastAPI(title="Polymarket Copy-Trader Profiles")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Store db on app state so require_auth can reach it without circular imports
    app.state.db = db

    # Session middleware must be added before any route that uses request.session
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.oauth_session_secret or "dev-secret-change-me",
        same_site="lax",
        https_only=False,
    )

    # Register OAuth routes (/login, /auth/google, /auth/github, /logout)
    oauth = configure_oauth(settings)
    app.include_router(make_auth_router(db, settings, oauth))

    # Redirect NeedsLogin exceptions to the login page
    @app.exception_handler(NeedsLogin)
    async def _needs_login_handler(request: Request, exc: NeedsLogin):
        return RedirectResponse(exc.url, status_code=303)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/profiles")

    @app.get("/calibration", response_class=HTMLResponse)
    async def calibration_view(
        request: Request,
        current_user: dict = Depends(require_auth),
    ):
        import json as _json

        buckets = await db.get_calibration_buckets()
        # Aggregate bias-tag performance from resolved signals
        from collections import defaultdict

        resolved = await db.get_resolved_signals(limit=5000)
        bias_agg: dict[str, dict[str, float]] = defaultdict(
            lambda: {"n": 0, "wins": 0, "brier_sum": 0.0}
        )
        for s in resolved:
            if s.was_correct is None:
                continue
            try:
                tags = _json.loads(s.bias_tags_json or "[]")
            except _json.JSONDecodeError:
                tags = []
            for tag in tags:
                agg = bias_agg[tag]
                agg["n"] += 1
                agg["wins"] += int(s.was_correct or 0)
                agg["brier_sum"] += float(s.realized_brier or 0.0)
        bias_rows = [
            {
                "tag": tag,
                "n": v["n"],
                "hit_rate": (v["wins"] / v["n"]) if v["n"] else 0.0,
                "brier": (v["brier_sum"] / v["n"]) if v["n"] else 0.0,
            }
            for tag, v in sorted(bias_agg.items(), key=lambda kv: -kv[1]["n"])
        ]
        return templates.TemplateResponse(
            request,
            "calibration.html",
            {
                "buckets": buckets,
                "bias_rows": bias_rows,
                "edge_mode": settings.edge_mode,
                "active_sources": settings.resolve_sources(),
                "current_user": current_user,
            },
        )

    @app.get("/profiles", response_class=HTMLResponse)
    async def list_profiles(
        request: Request,
        current_user: dict = Depends(require_auth),
    ):
        traders = await db.get_tracked_traders()
        rows = []
        for t in traders:
            mode = t.status if t.status in ("paper", "live", "shadow") else "shadow"
            perf = await recompute_for_wallet(
                db,
                t.wallet,
                mode=mode,
                throttle_secs=settings.copy_report_refresh_throttle_secs,
                mark_to_market=mark_to_market,
            )
            rows.append({"trader": t, "perf": perf})
        return templates.TemplateResponse(
            request,
            "profiles.html",
            {
                "rows": rows,
                "presets": list(PRESETS.keys()),
                "statuses": list(TRADER_STATUSES),
                "current_user": current_user,
            },
        )

    @app.get("/profiles/{wallet}", response_class=HTMLResponse)
    async def profile_detail(
        wallet: str,
        request: Request,
        current_user: dict = Depends(require_auth),
    ):
        wallet = wallet.lower()
        trader = await db.get_tracked_trader(wallet)
        if trader is None:
            raise HTTPException(status_code=404, detail="trader not found")
        mode = trader.status if trader.status in ("paper", "live", "shadow") else "shadow"
        perf = await recompute_for_wallet(
            db,
            wallet,
            mode=mode,
            throttle_secs=settings.copy_report_refresh_throttle_secs,
            mark_to_market=mark_to_market,
        )
        backtest = await db.get_copy_performance(wallet, "backtest")
        leader_trades = await db.get_leader_trades(wallet, limit=50)
        paper_orders = await db.get_paper_orders(wallet=wallet, limit=50)
        order_by_tx = {o.leader_tx_hash: o for o in paper_orders if o.leader_tx_hash}
        alerts = await db.get_audit_alerts(wallet, limit=20)
        promotable, reason = _can_promote(trader, perf, settings)
        next_status = _next_status(trader.status)
        return templates.TemplateResponse(
            request,
            "profile.html",
            {
                "trader": trader,
                "perf": perf,
                "backtest": backtest,
                "leader_trades": leader_trades,
                "order_by_tx": order_by_tx,
                "alerts": alerts,
                "presets": list(PRESETS.keys()),
                "promotable": promotable,
                "promote_reason": reason,
                "next_status": next_status,
                "current_user": current_user,
            },
        )

    @app.post("/profiles/{wallet}/promote")
    async def promote(
        wallet: str,
        current_user: dict = Depends(require_auth),
    ):
        wallet = wallet.lower()
        trader = await db.get_tracked_trader(wallet)
        if trader is None:
            raise HTTPException(404, "trader not found")
        mode = trader.status if trader.status in ("paper", "live", "shadow") else "shadow"
        perf = await recompute_for_wallet(
            db,
            wallet,
            mode=mode,
            throttle_secs=settings.copy_report_refresh_throttle_secs,
            mark_to_market=mark_to_market,
            force=True,
        )
        ok, reason = _can_promote(trader, perf, settings)
        if not ok:
            raise HTTPException(409, reason)
        new_status = _next_status(trader.status)
        await db.set_trader_status(wallet, new_status)
        return RedirectResponse(url=f"/profiles/{wallet}", status_code=303)

    @app.post("/profiles/{wallet}/preset")
    async def set_preset(
        wallet: str,
        request: Request,
        current_user: dict = Depends(require_auth),
    ):
        wallet = wallet.lower()
        form = await request.form()
        preset = (form.get("preset") or "").strip()
        if preset not in PRESETS:
            raise HTTPException(400, f"unknown preset: {preset}")
        await db.set_trader_preset(wallet, preset)
        return RedirectResponse(url=f"/profiles/{wallet}", status_code=303)

    @app.post("/profiles/{wallet}/disable")
    async def disable(
        wallet: str,
        current_user: dict = Depends(require_auth),
    ):
        wallet = wallet.lower()
        await db.set_trader_status(wallet, "disabled")
        return RedirectResponse(url=f"/profiles/{wallet}", status_code=303)

    return app
