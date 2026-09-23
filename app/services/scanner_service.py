from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.analytics.analysis import analyze_frame
from app.analytics.derivatives import normalize_derivatives
from app.analytics.market_context import market_context
from app.config import Settings
from app.db.scanner_models import ScannerRun
from app.providers.futures import FuturesProvider
from app.scanner.domain import (
    Candle,
    Derivatives,
    FrameAnalysis,
    MarketContext,
    Readiness,
    RunRead,
    ScannerResult,
    StaleDataError,
    Timeframe,
)
from app.scanner.locking import database_scan_lock
from app.scanner.notifications import deliver_notifications
from app.scanner.repository import ScannerRepository
from app.scanner.scoring import build_result
from app.scanner.universe import filter_universe
from app.services.outcome_service import OutcomeService

logger = logging.getLogger(__name__)
TELEMETRY_KEYS = (
    "requests",
    "http_429",
    "http_418",
    "cache_hits",
    "cache_misses",
    "candle_cache_hits",
    "candle_cache_misses",
    "stale_data_skips",
    "market_failures",
    "ready_setups",
    "outcomes_updated",
    "outcomes_completed",
)
TIMEFRAMES: tuple[Timeframe, ...] = ("15m", "1h", "4h")


class ScannerRuntime:
    """One instance per process shared by API and scheduler, including provider cache and lock."""

    def __init__(self, provider: FuturesProvider, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.closing = False
        self.frame_history: dict[str, dict[str, FrameAnalysis]] = {}
        self.seeded = False

    async def aclose(self) -> None:
        self.closing = True
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.provider.aclose()


class ScannerBusyError(Exception):
    pass


class ScannerService:
    def __init__(self, session: Session, runtime: ScannerRuntime) -> None:
        self.session, self.runtime = session, runtime
        self.settings, self.provider = runtime.settings, runtime.provider
        self.repository = ScannerRepository(session)

    async def run(self, now: datetime | None = None) -> RunRead:
        if not self.settings.scanner_enabled:
            raise ValueError("Scanner disabled")
        if self.runtime.closing or self.runtime.lock.locked():
            raise ScannerBusyError("Scanner already running")
        async with self.runtime.lock:
            self.runtime.task = asyncio.current_task()
            try:
                bind = self.session.get_bind()
                engine = bind if isinstance(bind, Engine) else bind.engine
                with database_scan_lock(engine) as acquired:
                    if not acquired:
                        raise ScannerBusyError("Another worker owns the scanner")
                    return await self._run(now or datetime.now(UTC))
            finally:
                self.runtime.task = None

    async def _frames(
        self, symbol: str, now: datetime
    ) -> tuple[dict[str, FrameAnalysis], list[Candle]]:
        bars = await asyncio.gather(
            *(self.provider.candles(symbol, tf, now) for tf in TIMEFRAMES),
            return_exceptions=True,
        )
        history: list[list[Candle]] = []
        for response in bars:
            if isinstance(response, BaseException):
                raise response
            history.append(response)
        frames = await asyncio.to_thread(
            lambda: {
                str(tf): analyze_frame(
                    candles,
                    tf,
                    now,
                    self.settings,
                    self.runtime.frame_history.get(symbol, {}).get(tf),
                )
                for tf, candles in zip(TIMEFRAMES, history)
            }
        )
        self.runtime.frame_history[symbol] = frames
        return frames, [
            b for b in history[0] if b.close_time <= frames["15m"].candle.close_time
        ]

    async def _run(self, now: datetime) -> RunRead:
        started = time.monotonic()
        exchange = self.provider.exchange
        config = {
            k: v
            for k, v in self.settings.model_dump(mode="json").items()
            if (
                k.startswith(("scanner_", "outcome_", "calibration_"))
                or k == "fvg_min_atr"
            )
            and k != "scanner_telegram_chat_id"  # no delivery destinations in history
        }
        config["exchange"] = exchange
        # Reload durable checkpoints after a restart; end read transactions before HTTP.
        # Checkpoints from another exchange are never reused (no cross-exchange mixing).
        previous_run = self.repository.last_run()
        context_seed = (
            previous_run.config_json.get("context_frames", {})
            if previous_run
            and previous_run.config_json.get("exchange", "BINANCE") == exchange
            else {}
        )
        for symbol, frames in context_seed.items():
            self.runtime.frame_history.setdefault(
                symbol,
                {tf: FrameAnalysis.model_validate(f) for tf, f in frames.items()},
            )
        run = ScannerRun(started_at=now, config_json=config, errors={})
        self.session.add(run)
        self.session.commit()
        run_id = run.id
        logger.info(
            "Scanner start run=%s exchange=%s dry_run=%s",
            run_id,
            exchange,
            self.settings.scanner_dry_run,
        )
        errors: dict[str, str] = {}
        telemetry: Counter[str] = Counter()
        provider_before = Counter(getattr(self.provider, "stats", {}))
        try:
            universe_data = await asyncio.gather(
                self.provider.contracts(),
                self.provider.tickers(),
                return_exceptions=True,
            )
            contracts, tickers = universe_data
            if isinstance(contracts, BaseException):
                raise contracts
            if isinstance(tickers, BaseException):
                raise tickers
            universe = filter_universe(contracts, tickers, self.settings)
            run.universe_size = len(universe)
            logger.info("Scanner universe size=%s", len(universe))
            if not self.runtime.seeded:
                # Once per process: this window query scans all snapshot history.
                self.runtime.seeded = True
                for previous in self.repository.latest_results(
                    limit=self.settings.scanner_max_markets + 2, exchange=exchange
                ):
                    self.runtime.frame_history.setdefault(
                        previous.symbol, previous.frames
                    )
                self.session.commit()
            context_frames: dict[str, dict[str, FrameAnalysis]] = {}
            cached_frames = {}
            failed_context: set[str] = set()
            if self.settings.scanner_use_btc_context:
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    try:
                        cached_frames[symbol] = await self._frames(symbol, now)
                        context_frames[symbol] = cached_frames[symbol][0]
                    except Exception as exc:  # noqa: BLE001 - isolate providers; log only safe error types
                        failed_context.add(symbol)
                        errors["context:" + symbol] = type(exc).__name__
                        logger.warning(
                            "Scanner context failed symbol=%s error=%s",
                            symbol,
                            type(exc).__name__,
                        )
            run.config_json = {
                **config,
                "context_frames": {
                    symbol: {tf: f.model_dump(mode="json") for tf, f in frames.items()}
                    for symbol, frames in context_frames.items()
                },
            }
            context = (
                market_context(context_frames, self.settings)
                if self.settings.scanner_use_btc_context
                else MarketContext(explanation="Context disabled")
            )
            semaphore = asyncio.Semaphore(self.settings.scanner_concurrency)

            validation_bars: dict[str, list[Candle]] = {}

            async def analyze(symbol: str) -> ScannerResult | None:
                async with semaphore:
                    try:
                        if symbol in failed_context:
                            raise ValueError("Context data already failed this cycle")
                        frames, bars = (
                            cached_frames[symbol]
                            if symbol in cached_frames
                            else await self._frames(symbol, now)
                        )
                        validation_bars[symbol] = bars
                        observed_at = now + timedelta(
                            seconds=time.monotonic() - started
                        )
                        derivatives = Derivatives()
                        if self.settings.scanner_use_derivatives:
                            try:
                                raw = await self.provider.derivatives(
                                    symbol, observed_at
                                )
                                derivatives = normalize_derivatives(
                                    raw,
                                    bars,
                                    self.settings.scanner_funding_extreme,
                                    observed_at,
                                )
                            except Exception as exc:  # noqa: BLE001 - isolate providers; log only safe error types
                                derivatives.errors.append(type(exc).__name__)
                                logger.warning(
                                    "Scanner derivatives failed symbol=%s error=%s",
                                    symbol,
                                    type(exc).__name__,
                                )
                        result = build_result(
                            symbol,
                            frames,
                            derivatives,
                            context,
                            tickers[symbol],
                            observed_at,
                            self.settings,
                        )
                        result.exchange = exchange
                        return result
                    except Exception as exc:  # noqa: BLE001 - isolate providers; log only safe error types
                        errors[symbol] = type(exc).__name__
                        telemetry[
                            "stale_data_skips"
                            if isinstance(exc, StaleDataError)
                            else "market_failures"
                        ] += 1
                        logger.warning(
                            "Scanner market failed symbol=%s error=%s",
                            symbol,
                            type(exc).__name__,
                        )
                        return None

            results = await asyncio.gather(*(analyze(symbol) for symbol in universe))
            # No writes or open read transactions span public network requests.
            # One short transaction per market: a failure rolls back only that market,
            # and earlier snapshots survive a later error (no SQLite savepoint reliance).
            run.failed += sum(result is None for result in results)
            self.session.commit()
            for result in results:
                if result is None:
                    continue
                try:
                    self.repository.save(
                        run_id,
                        result,
                        self.settings,
                        validation_bars[result.symbol],
                    )
                    run.analyzed += 1
                    run.high_confluence += sum(
                        s.score >= self.settings.scanner_high_score
                        for s in result.setups
                    )
                    telemetry["ready_setups"] += sum(
                        s.readiness == Readiness.READY for s in result.setups
                    )
                    self.session.commit()
                except Exception as exc:  # noqa: BLE001 - isolate providers; log only safe error types
                    self.session.rollback()
                    errors[result.symbol] = type(exc).__name__
                    run.failed += 1
                    self.session.commit()
                    logger.warning(
                        "Scanner persistence failed symbol=%s error=%s",
                        result.symbol,
                        type(exc).__name__,
                    )
            try:
                telemetry.update(
                    await OutcomeService(
                        self.session, self.provider, self.settings
                    ).process(
                        now,
                        {
                            (symbol, "15m"): bars
                            for symbol, bars in validation_bars.items()
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - research tracking must not fail the scan
                self.session.rollback()
                errors["outcomes"] = type(exc).__name__
                logger.warning("Outcome tracking failed error=%s", type(exc).__name__)
            self.repository.expire(now + timedelta(seconds=time.monotonic() - started))
            run.status = "PARTIAL" if errors else "COMPLETED"
        except asyncio.CancelledError:
            run.status = "CANCELLED"
            errors["run"] = "CancelledError"
            raise
        except Exception as exc:  # noqa: BLE001 - isolate providers; log only safe error types
            self.session.rollback()
            reloaded = self.session.get(ScannerRun, run_id)
            assert reloaded is not None
            run = reloaded
            run.status = "FAILED"
            errors["run"] = type(exc).__name__
            logger.warning("Scanner run failed error=%s", type(exc).__name__)
        finally:
            provider_after = Counter(getattr(self.provider, "stats", {}))
            provider_after.subtract(provider_before)
            telemetry.update(+provider_after)
            run.telemetry = {
                "markets_requested": run.universe_size,
                "markets_analyzed": run.analyzed,
                "markets_failed": run.failed,
                "high_confluence": run.high_confluence,
                "dry_run": self.settings.scanner_dry_run,
                "exchange": exchange,
                **{k: telemetry.get(k, 0) for k in TELEMETRY_KEYS},
                **telemetry,
            }
            run.errors = errors
            run.completed_at = datetime.now(UTC)
            run.duration_seconds = time.monotonic() - started
            self.session.commit()
            logger.info(
                "Scanner complete status=%s analyzed=%s failed=%s high_confluence=%s elapsed=%.2fs telemetry=%s",
                run.status,
                run.analyzed,
                run.failed,
                run.high_confluence,
                run.duration_seconds,
                run.telemetry,
            )
        if self.settings.scanner_send_telegram:
            try:
                sent = await deliver_notifications(self.session, self.settings, now)
                if sent:
                    logger.info("Scanner Telegram alerts delivered=%s", sent)
            except Exception as exc:  # noqa: BLE001 - alerts must never fail a persisted scan
                self.session.rollback()
                logger.warning(
                    "Scanner Telegram delivery failed error=%s", type(exc).__name__
                )
        return RunRead.model_validate(run)
