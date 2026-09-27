from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.analytics.analysis import analyze_frame
from app.analytics.derivatives import normalize_derivatives
from app.analytics.market_context import market_context
from app.analytics.open_interest import (
    HORIZONS,
    history_points,
)
from app.config import Settings
from app.db.scanner_models import ScannerRun
from app.providers.futures import FuturesProvider
from app.providers.futures_factory import ProviderRegistry
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
from app.scanner.open_interest_repository import load_history as load_oi_history
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
# Longest OI horizon plus one boundary of slack for the anchoring bucket.
OI_LOOKBACK = max(HORIZONS.values()) + timedelta(minutes=30)
TIMEFRAMES: tuple[Timeframe, ...] = ("15m", "1h", "4h")


class ScannerRuntime:
    """One instance per process shared by API and scheduler, including provider cache and lock."""

    def __init__(
        self,
        provider: FuturesProvider,
        settings: Settings,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.provider = provider  # the scanning exchange for new setups
        self.settings = settings
        # Providers for every exchange that still has setups to track.
        self.registry = registry or ProviderRegistry(settings)
        self.registry.register(provider)
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.closing = False
        self.frame_history: dict[str, dict[str, FrameAnalysis]] = {}
        self.seeded = False
        self.oi_lock = asyncio.Lock()
        self.oi_task: asyncio.Task | None = None
        self.oi_last_collection: dict = {}
        # Serialises every scanner-alert delivery pass (scan, lifecycle tick) so the
        # same due alert can never be sent twice by overlapping passes.
        self.notify_lock = asyncio.Lock()
        self.setup_listeners: list[Callable[[], None]] = []

    def setups_changed(self) -> None:
        """Setup rows changed: event-driven consumers (news index) refresh lazily."""
        for listener in self.setup_listeners:
            listener()

    async def aclose(self) -> None:
        self.closing = True
        tasks = [
            task
            for task in (self.task, self.oi_task)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.registry.aclose()


class RunNotifier:
    """Delivers due scanner alerts DURING a scan, as soon as each symbol's closed-candle
    result is persisted, instead of after the whole universe. Alert rules, cooldowns and
    the per-run cap are unchanged: the cap is a budget shared by all passes of a run."""

    def __init__(self, service: ScannerService, now: datetime) -> None:
        self.service, self.now = service, now
        self.enabled = service.settings.scanner_send_telegram
        self.remaining = service.settings.scanner_alerts_per_run
        self.sent = 0
        self._event = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task | None = None

    def poke(self) -> None:
        if not self.enabled:
            return
        self._event.set()
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self._event.wait()
            self._event.clear()
            await self._pass()
            if self._closing and not self._event.is_set():
                return

    async def _pass(self) -> None:
        if self.remaining <= 0:
            return
        runtime, settings = self.service.runtime, self.service.settings
        try:
            async with runtime.notify_lock:
                sent = await deliver_notifications(
                    self.service.session, settings, self.now, limit=self.remaining
                )
            self.remaining -= sent
            self.sent += sent
        except Exception as exc:  # noqa: BLE001 - alerts must never fail a persisted scan
            self.service.session.rollback()
            logger.warning(
                "Scanner Telegram delivery failed error=%s", type(exc).__name__
            )

    async def close(self) -> None:
        """Final pass (e.g. expiries found at the end of the run), then stop."""
        if not self.enabled:
            return
        self._closing = True
        self.poke()
        if self._task is not None:
            await self._task
        if self.sent:
            logger.info("Scanner Telegram alerts delivered=%s", self.sent)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


async def lifecycle_tick(runtime: ScannerRuntime, session: Session) -> int:
    """Between scans: expire due setups and send due lifecycle alerts immediately
    (EXPIRED no longer waits for the next 5-minute scan). Same notification rules."""
    now = datetime.now(UTC)
    ScannerRepository(session).expire(now)
    session.commit()
    runtime.setups_changed()
    if not runtime.settings.scanner_send_telegram:
        return 0
    async with runtime.notify_lock:
        return await deliver_notifications(session, runtime.settings, now)


class ScannerBusyError(Exception):
    pass


class ScannerService:
    def __init__(self, session: Session, runtime: ScannerRuntime) -> None:
        self.session, self.runtime = session, runtime
        self.settings, self.provider = runtime.settings, runtime.provider
        self.repository = ScannerRepository(session)

    async def run(
        self, now: datetime | None = None, only: str | None = None
    ) -> RunRead:
        """Full scan, or a PRIORITY RESCAN of one symbol (`only`): same closed-candle
        analysis, persistence, locks and alert rules; outcome tracking is skipped."""
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
                    return await self._run(now or datetime.now(UTC), only)
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

    async def _run(self, now: datetime, only: str | None = None) -> RunRead:
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
        if only:
            config["priority_rescan"] = only
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
        provider_before = self.runtime.registry.stats()
        notifier = RunNotifier(self, now)
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
            if only:
                # One symbol only; the watcher already applied its liquidity filter.
                tradable = {c.symbol for c in contracts if c.status == "TRADING"}
                universe = [only] if only in tradable and only in tickers else []
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
            # Exchanges without public OI history get their own stored observations.
            # Loaded before any HTTP so no read transaction spans network calls.
            self_recorded_oi = (
                self.settings.scanner_use_derivatives
                and not self.provider.publishes_oi_history
            )
            stored_oi = (
                load_oi_history(
                    self.session,
                    exchange,
                    universe,
                    now - OI_LOOKBACK,
                )
                if self_recorded_oi
                else {}
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
                                if self_recorded_oi and hasattr(
                                    self.provider, "funding"
                                ):
                                    raw = await self.provider.funding(
                                        symbol, observed_at
                                    )
                                    candidates = [
                                        o
                                        for o in stored_oi.get(symbol, {}).values()
                                        if o.observed_at <= observed_at
                                        and o.bucket_at <= observed_at
                                    ]
                                    if candidates:
                                        latest = max(
                                            candidates, key=lambda o: o.observed_at
                                        )
                                        raw.open_interest = latest.open_interest
                                        raw.open_interest_notional = (
                                            latest.open_interest_notional
                                        )
                                        raw.oi_timestamp = latest.observed_at
                                else:
                                    raw = await self.provider.derivatives(
                                        symbol, observed_at
                                    )
                                observed_at = now + timedelta(
                                    seconds=time.monotonic() - started
                                )
                                if self_recorded_oi:
                                    raw.history = history_points(
                                        stored_oi.get(symbol, {}), as_of=observed_at
                                    )
                                    raw.oi_history_source = "SELF_RECORDED"
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

            def persist(result: ScannerResult) -> bool:
                # One short transaction per market, committed as soon as its
                # closed-candle analysis is done: a failure rolls back only that
                # market, and no transaction spans a network request.
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
                    return True
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
                    return False

            async def analyze_and_persist(symbol: str) -> ScannerResult | None:
                result = await analyze(symbol)
                if result is not None and persist(result):
                    self.runtime.setups_changed()
                    notifier.poke()  # READY/INVALIDATED alerts go out immediately
                return result

            results = await asyncio.gather(
                *(analyze_and_persist(symbol) for symbol in universe)
            )
            run.failed += sum(result is None for result in results)
            self.session.commit()
            # Expiries are known now: deliver them before slower outcome tracking.
            self.repository.expire(now + timedelta(seconds=time.monotonic() - started))
            self.session.commit()
            self.runtime.setups_changed()
            notifier.poke()
            # Priority rescans leave outcome tracking to the regular cycle.
            if not only:
                try:
                    telemetry.update(
                        await OutcomeService(
                            self.session, self.runtime.registry, self.settings
                        ).process(
                            now,
                            {
                                (exchange, symbol, "15m"): bars
                                for symbol, bars in validation_bars.items()
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - research tracking must not fail the scan
                    self.session.rollback()
                    errors["outcomes"] = type(exc).__name__
                    logger.warning(
                        "Outcome tracking failed error=%s", type(exc).__name__
                    )
            run.status = "PARTIAL" if errors else "COMPLETED"
        except asyncio.CancelledError:
            notifier.cancel()
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
            provider_after = self.runtime.registry.stats()
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
        await notifier.close()
        return RunRead.model_validate(run)
