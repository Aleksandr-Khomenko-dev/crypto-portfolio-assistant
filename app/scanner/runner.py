"""Standalone 24/7 scanner loop for research data collection.

Usage:
    python -m app.scanner.runner          # run every CPDA_SCANNER_INTERVAL_MINUTES
    python -m app.scanner.runner --once   # one cycle, then exit (smoke test)

Public market data only. Never trades and never places orders. Runs the scanner (with
outcome tracking) and, for BingX, the clock-aligned OI collector; portfolio jobs and
bot polling stay off.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import inspect

from app.analytics.open_interest import next_boundary
from app.api.deps import close_scanner_runtime, get_scanner_runtime
from app.config import Settings, get_settings
from app.db.session import get_db_session, get_engine, get_session_factory
from app.services.oi_collector_service import OICollectorService
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


Clock = Callable[[], datetime]


async def collect_oi_forever(
    stop: asyncio.Event,
    collect: Callable[[datetime], Awaitable[dict]],
    clock: Clock = lambda: datetime.now(UTC),
) -> None:
    """Target every wall-clock 15m boundary (:00/:15/:30/:45) independently of scans.

    The next target is recomputed from the wall clock each time (no drifting
    "sleep 900 s after finishing"). A restart at 10:07 targets 10:15; a missed
    boundary is never collected late or relabelled.
    """
    while not stop.is_set():
        target = next_boundary(clock())
        # asyncio timers run on the monotonic clock and may fire a few ms before the
        # wall-clock boundary (observed live: 6-42 ms early), which made the collector
        # refuse the target and lose ~31% of boundaries. Wait until it really arrived.
        while (delay := (target - clock()).total_seconds()) > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay + 0.001)
                return  # stop requested while waiting
            except TimeoutError:
                pass
        try:
            await collect(target)
        except Exception as exc:  # noqa: BLE001 - keep sampling future boundaries
            logger.error("OI collection failed error=%s", type(exc).__name__)


async def lifecycle_forever(stop: asyncio.Event, interval: float) -> None:
    """Expire setups and send due lifecycle alerts between scans (event-driven-ish:
    bounded by `interval`, never by the 5-minute scan cadence)."""
    from app.services.scanner_service import lifecycle_tick

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            with get_db_session() as session:
                await lifecycle_tick(get_scanner_runtime(), session)
        except Exception as exc:  # noqa: BLE001 - keep ticking
            logger.error("Lifecycle tick failed error=%s", type(exc).__name__)


async def log_news_status(stop: asyncio.Event, pipeline, every: float = 300) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
            return
        except TimeoutError:
            logger.info("News status %s", pipeline.snapshot())


async def start_fast_watcher(settings):
    """Realtime early-warning watcher (never changes scores or scanner alert rules)."""
    from app.fast.watcher import FastMarketWatcher, chat_fingerprint
    from app.services.scanner_service import ScannerService
    from app.services.telegram_service import TelegramService

    runtime = get_scanner_runtime()
    telegram = TelegramService(settings)
    chat = settings.scanner_telegram_chat_id

    async def sender(text, reply_to, buttons):
        return await telegram.send_message(chat, text, reply_to, buttons)

    async def rescan(symbol: str) -> None:
        # Closed-candle PRIORITY RESCAN of one symbol through the normal scanner path.
        with get_db_session() as session:
            await ScannerService(session, runtime).run(only=symbol)

    watcher = FastMarketWatcher(
        settings,
        get_session_factory(),
        runtime.registry.get("BINGX"),
        sender if telegram.enabled and chat else None,
        rescan,
        mode="DRY_RUN" if settings.scanner_dry_run else "LIVE",
        # Read-only view of the scanner's latest CLOSED-candle frames (zones/ATR);
        # the early layer never writes the scanner's checkpoints.
        frames_source=lambda symbol: runtime.frame_history.get(symbol),
        destination=chat_fingerprint(chat),
    )
    symbols = await watcher.start()
    logger.info(
        "Fast watcher subscribed symbols=%s connections=%s early=%s",
        len(symbols),
        len(watcher.connections),
        settings.early_enabled,
    )
    return watcher


async def maybe_start_fast_watcher(settings, start=None):
    """The scanner must keep running even if the watcher cannot start."""
    try:
        return await (start or start_fast_watcher)(settings)
    except Exception as exc:  # noqa: BLE001 - early-warning layer is optional
        logger.error("Fast watcher failed to start error=%s", type(exc).__name__)
        return None


async def log_fast_status(stop: asyncio.Event, watcher, every: float = 300) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
            return
        except TimeoutError:
            logger.info("Fast watcher status %s", watcher.snapshot())


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
    collector: asyncio.Task | None = None
    if (
        not once
        and settings.oi_collection_enabled
        and settings.scanner_provider == "bingx"
    ):
        # Same runtime as the scan cycles: one BingX provider, cache and rate limiter.
        service = OICollectorService(get_scanner_runtime(), get_session_factory())
        collector = asyncio.create_task(collect_oi_forever(stop, service.collect))
        logger.info("OI collector enabled at :00/:15/:30/:45")
    background: list[asyncio.Task] = []
    pipeline = None
    if not once:
        background.append(
            asyncio.create_task(
                lifecycle_forever(stop, settings.lifecycle_check_seconds)
            )
        )
        if settings.news_enabled:
            from app.news import service as news_service

            pipeline, providers = news_service.build_pipeline(
                settings, get_scanner_runtime()
            )
            await pipeline.start(providers)
            news_service.RUNNING = pipeline
            background.append(asyncio.create_task(log_news_status(stop, pipeline)))
            logger.info(
                "News pipeline started providers=%s (context only; scores unchanged)",
                [p.name for p in providers],
            )
    watcher = None
    if not once and settings.fast_enabled and settings.scanner_provider == "bingx":
        watcher = await maybe_start_fast_watcher(settings)
        if watcher is not None:
            background.append(asyncio.create_task(log_fast_status(stop, watcher)))
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
        stop.set()
        if watcher is not None:
            await watcher.stop()
        if pipeline is not None:
            await pipeline.stop()  # drains accepted CRITICAL items first
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        if collector is not None:
            # A collection in progress is cancelled; its persistence step is shielded
            # and finishes before the runtime (and its DB/HTTP clients) close.
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
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
