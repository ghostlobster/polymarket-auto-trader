"""
Polymarket Auto-Trader — entry point.

Usage:
    python main.py                        # Run trading loop (uses .env for config)
    DRY_RUN=true python main.py           # Dry-run mode (no real orders)
    COPY_ENABLED=true python main.py      # Add the copy-trading subsystem in parallel
"""
import asyncio
import contextlib
import signal as signal_module
from datetime import datetime

import httpx
import structlog

from agents import OrchestratorAgent
from config import Settings
from database import init_db
from polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)


def configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), level.upper(), 20)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def thesis_loop(orchestrator, settings, shutdown):
    """Existing 15-minute research-driven trading cycle."""
    cycle_num = 0
    while not shutdown.is_set():
        cycle_num += 1
        log.info("Thesis cycle starting", cycle=cycle_num)
        try:
            summary = await orchestrator.run_cycle()
            log.info(
                "Thesis cycle complete", cycle=cycle_num,
                opportunities=summary["opportunities_found"],
                signals=summary["signals_generated"],
                trades=summary["trades_executed"],
            )
        except Exception as exc:
            log.error("Thesis cycle failed", cycle=cycle_num, error=str(exc))

        if shutdown.is_set():
            break

        interval_secs = settings.scan_interval_minutes * 60
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_secs)
        except asyncio.TimeoutError:
            pass


async def copy_loop(copy_agent, settings, shutdown):
    """Tight-cadence loop polling tracked traders for new trades to copy."""
    while not shutdown.is_set():
        try:
            summary = await copy_agent.cycle()
            if summary.get("events"):
                log.info("Copy cycle", **summary)
        except Exception as exc:
            log.error("Copy cycle failed", error=str(exc))
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=settings.copy_poll_seconds)
        except asyncio.TimeoutError:
            pass


async def discovery_loop(discovery_agent, settings, shutdown):
    """Daily-cadence loop refreshing the leaderboard of tracked traders."""
    # First run on startup so users see results immediately.
    while not shutdown.is_set():
        try:
            kept = await discovery_agent.discover()
            log.info("Discovery cycle complete", kept=len(kept))
        except Exception as exc:
            log.error("Discovery failed", error=str(exc))
        interval = settings.leaderboard_refresh_hours * 3600
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def audit_loop(audit_agent, settings, shutdown):
    """Periodic audit reconciling expected copies vs actual orders."""
    while not shutdown.is_set():
        try:
            summary = await audit_agent.cycle()
            if summary.get("alerts") or summary.get("demoted"):
                log.warning("Audit findings", **summary)
        except Exception as exc:
            log.error("Audit failed", error=str(exc))
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=settings.copy_audit_interval_secs)
        except asyncio.TimeoutError:
            pass


async def web_server_task(app, host: str, port: int, shutdown):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await shutdown.wait()
    server.should_exit = True
    with contextlib.suppress(Exception):
        await server_task


async def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)

    log.info(
        "Polymarket Auto-Trader starting",
        dry_run=settings.dry_run,
        scan_interval_minutes=settings.scan_interval_minutes,
        max_position_usdc=settings.max_position_usdc,
        copy_enabled=settings.copy_enabled,
    )
    if settings.dry_run:
        log.warning("DRY RUN MODE — no real orders will be placed")
    if not settings.polymarket_private_key:
        log.warning(
            "POLYMARKET_PRIVATE_KEY not set. Run: python -m polymarket.auth setup"
        )

    db = await init_db(settings.db_path)
    poly = PolymarketClient(settings)
    orchestrator = OrchestratorAgent(settings, poly, db)

    shutdown = asyncio.Event()

    def _handle_signal(sig, frame):
        log.info("Shutdown signal received", signal=sig)
        shutdown.set()

    signal_module.signal(signal_module.SIGINT, _handle_signal)
    signal_module.signal(signal_module.SIGTERM, _handle_signal)

    tasks = [asyncio.create_task(thesis_loop(orchestrator, settings, shutdown))]

    http_client = None
    if settings.copy_enabled:
        from agents import CopyAuditAgent, CopyTraderAgent, TraderDiscoveryAgent
        from polymarket.data_client import PolymarketDataClient

        http_client = httpx.AsyncClient(timeout=15.0)
        data_client = PolymarketDataClient(settings.polymarket_data_api, http=http_client)

        discovery = TraderDiscoveryAgent(settings, data_client, db)
        copy_agent = CopyTraderAgent(settings, data_client, poly, db, risk=orchestrator.risk_mgr)

        async def mark_to_market(wallet: str) -> None:
            broker = copy_agent.executor.broker(wallet)
            await broker.mark_to_market()

        audit = CopyAuditAgent(settings, db, mark_to_market=mark_to_market)

        tasks.append(asyncio.create_task(discovery_loop(discovery, settings, shutdown)))
        tasks.append(asyncio.create_task(copy_loop(copy_agent, settings, shutdown)))
        tasks.append(asyncio.create_task(audit_loop(audit, settings, shutdown)))

        if settings.copy_web_enabled:
            from web.server import build_app
            app = build_app(db, copy_agent, mark_to_market)
            tasks.append(asyncio.create_task(
                web_server_task(app, settings.copy_web_host, settings.copy_web_port, shutdown)
            ))
            log.info(
                "Copy-trader web UI started",
                url=f"http://{settings.copy_web_host}:{settings.copy_web_port}/profiles",
            )

    log.info("Loops running", count=len(tasks))

    await shutdown.wait()
    log.info("Shutting down", started_at=datetime.utcnow().isoformat())

    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(BaseException):
            await t

    if http_client is not None:
        await http_client.aclose()
    await db.close()
    log.info("Polymarket Auto-Trader stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
