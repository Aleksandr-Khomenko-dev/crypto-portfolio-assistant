"""Standalone 24/7 scanner loop for research data collection.

Usage:
    python -m app.scanner.runner          # run every CPDA_SCANNER_INTERVAL_MINUTES
    python -m app.scanner.runner --once   # one cycle, then exit (smoke test)

Public Binance USD-M market data only. Never trades and never places orders. Runs
only the scanner (with outcome tracking); portfolio jobs and bot polling stay off.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time

from sqlalchemy import inspect

from app.api.deps import close_scanner_runtime, get_scanner_runtime
from app.config import Settings, get_settings
from app.db.session import get_db_session, get_engine
from app.services.scanner_service import ScannerBusyError, ScannerService

logger = logging.getLogger("app.scanner.runner")


def check_schema(settings: Settings) -> None:
    tables = set(inspect(get_engine(settings.database_url)).get_table_names())
    missing = {"scanner_runs", "market_setups", "setup_outcomes"} - tables
    if missing:
        raise SystemExit(
            f"Database is missing {sorted(missing)}; run `python -m alembic upgrade head`."
        )


async def run_cycle() -> None:
    try:
        with get_db_session() as session:
            run = await ScannerService(session, get_scanner_runtime()).run()
        logger.info(
            "Cycle %s analyzed=%s/%s ready=%s telemetry=%s",
            run.status,
            run.analyzed,
            run.universe_size,
            run.telemetry.get("ready_setups", 0),
            run.telemetry,
        )
    except ScannerBusyError:
        logger.info("Cycle skipped: another scan is running")
    except Exception as exc:  # noqa: BLE001 - keep collecting; log only the error type
        logger.error("Cycle failed error=%s", type(exc).__name__)


async def main(once: bool) -> None:
    settings = get_settings()
    if not settings.scanner_enabled:
        raise SystemExit("Scanner disabled: set CPDA_SCANNER_ENABLED=true")
    check_schema(settings)
    logger.info(
        "Scanner runner start dry_run=%s send_telegram=%s interval=%smin (read-only, no trading)",
        settings.scanner_dry_run,
        settings.scanner_send_telegram,
        settings.scanner_interval_minutes,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            started = time.monotonic()
            await run_cycle()
            if once:
                break
            delay = settings.scanner_interval_minutes * 60 - (
                time.monotonic() - started
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, delay))
            except TimeoutError:
                pass
    finally:
        await close_scanner_runtime()
        logger.info("Scanner runner stopped")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(main(args.once))


if __name__ == "__main__":
    cli()
