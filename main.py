"""
Polymarket Auto-Trader — entry point.

Usage:
    python main.py               # Run trading loop (uses .env for config)
    DRY_RUN=true python main.py  # Dry-run mode (no real orders)
"""
import asyncio
import signal

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


async def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)

    log.info(
        "Polymarket Auto-Trader starting",
        dry_run=settings.dry_run,
        scan_interval_minutes=settings.scan_interval_minutes,
        max_position_usdc=settings.max_position_usdc,
    )

    if settings.dry_run:
        log.warning("DRY RUN MODE — no real orders will be placed")

    if not settings.polymarket_private_key:
        log.warning(
            "POLYMARKET_PRIVATE_KEY not set. "
            "Run: python -m polymarket.auth setup"
        )

    db = await init_db(settings.db_path)
    poly = PolymarketClient(settings)
    orchestrator = OrchestratorAgent(settings, poly, db)

    # Graceful shutdown on SIGINT/SIGTERM
    shutdown = asyncio.Event()

    def _handle_signal(sig, frame):
        log.info("Shutdown signal received", signal=sig)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle_num = 0
    while not shutdown.is_set():
        cycle_num += 1
        log.info("Starting trading cycle", cycle=cycle_num)
        try:
            summary = await orchestrator.run_cycle()
            log.info(
                "Cycle complete",
                cycle=cycle_num,
                opportunities=summary["opportunities_found"],
                signals=summary["signals_generated"],
                trades=summary["trades_executed"],
            )
        except Exception as exc:
            log.error("Cycle failed", cycle=cycle_num, error=str(exc))

        if shutdown.is_set():
            break

        interval_secs = settings.scan_interval_minutes * 60
        log.info("Waiting for next cycle", next_in_seconds=interval_secs)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_secs)
        except asyncio.TimeoutError:
            pass  # Normal — time to run next cycle

    await db.close()
    log.info("Polymarket Auto-Trader stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
