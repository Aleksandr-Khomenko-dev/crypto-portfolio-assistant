"""FastMarketWatcher: realtime early-warning layer beside the closed-candle scanner.

Independent of the 5-minute scan, 15m closes, OI boundaries and news polling. It
never changes a technical score, threshold or scanner cooldown: it only sends its own,
clearly labelled alerts and requests a closed-candle PRIORITY RESCAN of one symbol.

Two detectors share the same stream and infrastructure:
* fast moves (adaptive volatility thresholds, `app.fast.detector`);
* early structure (zones, ignition, formations, breakout/retest episodes,
  `app.early.engine`) on top of a closed-candle context refreshed every 5m close.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.scanner_models import (
    FastMarketEvent,
    MarketSetup,
    OpenInterestSnapshot,
    ScannerSnapshot,
)
from app.early import service as early_service
from app.early.engine import EarlyEngine
from app.early.model import PRIORITY, RESCAN_EVENTS, EarlyEvent, EventType, KeyZone
from app.early.outcomes import invalidation_for
from app.early.policy import Decision, NotificationPolicy
from app.fast.detector import (
    AntiSpam,
    FastConfig,
    FastState,
    SymbolTracker,
    Trigger,
    evaluate,
)
from app.fast.stream import StreamConnection
from app.news.metrics import Samples
from app.news.pipeline import BoundedPriorityQueue
from app.providers.bingx_futures import (
    bingx_to_internal_symbol,
    internal_to_bingx_symbol,
)
from app.telegram.early import EarlyContext, format_early_event
from app.telegram.fast import FastContext, format_fast_event, tradingview_url
from app.telegram.scanner import Mode

logger = logging.getLogger(__name__)
Sender = Callable[[str, int | None, list[tuple[str, str]]], Awaitable[list[int]]]
Rescan = Callable[[str], Awaitable[None]]
FAST_STATES = {state.value for state in FastState}


def config_from(settings: Settings) -> FastConfig:
    base = FastConfig()
    return FastConfig(
        min_pct={
            **base.min_pct,
            "1m": settings.fast_min_pct_1m,
            "3m": settings.fast_min_pct_3m,
            "5m": settings.fast_min_pct_5m,
        },
        extreme_pct=base.extreme_pct,
        sigma_k=settings.fast_sigma_k,
        sigma_k_extreme=settings.fast_sigma_k_extreme,
        volume_expansion=settings.fast_volume_expansion,
        cooldown_seconds=settings.fast_cooldown_minutes * 60,
    )


def chat_fingerprint(chat_id: str | None) -> str | None:
    """Stable, non-reversible destination label for logs (never the chat id)."""
    if not chat_id:
        return None
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:10]


@dataclass
class FastJob:
    symbol: str
    trigger: Trigger
    decision: str
    received: float  # epoch seconds (stream receipt)
    detected: float
    stages: dict[str, float] = field(default_factory=dict)
    event_type: str = EventType.FAST_MOVE.value
    extra_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class EarlyJob:
    event: EarlyEvent
    received: float  # stream receipt, or context build time for closed-candle events
    detected: float


def _notify(decision: Decision) -> dict[str, Any]:
    return {
        "eligible": decision.send,
        "reason": decision.reason,
        "detail": decision.detail,
        "reply_to": decision.reply_to,
        "episode": decision.episode_key,
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class FastMarketWatcher:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        bingx,  # BingXFuturesProvider: shared REST client + rate limiter
        sender: Sender | None,
        rescan: Rescan | None = None,
        *,
        mode: Mode = "LIVE",
        clock: Callable[[], float] = time.time,
        frames_source: early_service.FramesSource | None = None,
        destination: str | None = None,
    ) -> None:
        self.settings, self.sessions, self.bingx = settings, sessions, bingx
        self.sender, self.rescan_symbol, self.mode, self.clock = (
            sender,
            rescan,
            mode,
            clock,
        )
        self.destination = destination  # safe fingerprint only
        self.config = config_from(settings)
        self.antispam = AntiSpam(self.config)
        self.trackers: dict[str, SymbolTracker] = {}
        self.outbox = BoundedPriorityQueue(settings.fast_queue_size)
        self.rescans: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
        self._pending_rescans: set[str] = set()
        self._rescan_times: deque[float] = deque()
        self.analysis_times: dict[str, dict[str, float]] = {}
        self.connections: list[StreamConnection] = []
        self.counters: Counter[str] = Counter()
        self.exclusions: dict[str, str] = {}
        self.spreads: dict[str, float] = {}
        self.latency = {
            key: Samples()
            for key in (
                "fast_detection_ms",
                "fast_to_telegram_ms",
                "priority_analysis_ms",
            )
        }
        self.early_cfg = early_service.config_from(settings)
        self.early: EarlyEngine | None = (
            EarlyEngine(
                self.early_cfg,
                patterns_enabled=settings.early_patterns_enabled,
                zone_watch_enabled=settings.early_zone_watch_enabled,
                formation_min_strength=settings.early_formation_min_strength,
            )
            if settings.early_enabled
            else None
        )
        # Detection and notification are separate: every valid event is persisted
        # (and enters outcome research); this policy only decides who is told.
        self.policy = NotificationPolicy(early_service.policy_config_from(settings))
        self.contexts = early_service.ContextBuilder(
            settings, bingx, sessions, self.early_cfg, frames_source
        )
        self.frames_source = frames_source
        self._primed: set[str] = set()
        self._last_early: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---- universe -----------------------------------------------------------------

    def _priority_symbols(self) -> tuple[set[str], set[str]]:
        from app.news.entities import portfolio_pairs

        with self.sessions() as session:
            active = set(
                session.scalars(
                    select(MarketSetup.symbol).where(MarketSetup.lifecycle == "ACTIVE")
                )
            )
            return set(portfolio_pairs(session)), active

    def _near_zone(self, symbol: str) -> bool:
        frames = self.frames_source(symbol) if self.frames_source else None
        frame = (frames or {}).get("1h")
        if frame is None:
            return False
        nearest = [z.distance_atr for z in (frame.supports[:1] + frame.resistances[:1])]
        return bool(nearest) and min(nearest) <= 0.5

    async def universe(self) -> list[str]:
        """Liquid BingX USDT perpetuals (not only news tiers). Order of attention:
        portfolio, active setups, near an important zone, then by liquidity. Every
        excluded market is recorded with its reason."""
        contracts, tickers = await asyncio.gather(
            self.bingx.contracts(), self.bingx.tickers()
        )
        portfolio, active = await asyncio.to_thread(self._priority_symbols)
        priority = portfolio | active
        floor = self.settings.fast_min_quote_volume_usd
        max_spread = self.settings.fast_max_spread_pct
        chosen = []
        self.exclusions = {}
        for contract in contracts:
            symbol = contract.symbol
            reason = None
            ticker = tickers.get(symbol)
            if contract.status != "TRADING":
                reason = "NOT_TRADING"
            elif contract.quote_asset != "USDT" or contract.underlying_type != "COIN":
                reason = "NOT_USDT_COIN_PERPETUAL"
            elif ticker is None:
                reason = "NO_TICKER"
            else:
                volume = float(ticker.quote_volume)
                spread = ticker.spread_pct
                # Portfolio/active-setup symbols get a lower (but non-zero) floor.
                limit = floor / 10 if symbol in priority else floor
                if volume < limit:
                    reason = "LOW_QUOTE_VOLUME"
                elif spread is not None and spread > max_spread * (
                    2 if symbol in priority else 1
                ):
                    reason = "WIDE_SPREAD"
                else:
                    if spread is not None:
                        self.spreads[symbol] = spread
                    rank = (
                        0
                        if symbol in portfolio
                        else 1
                        if symbol in active
                        else 2
                        if self._near_zone(symbol)
                        else 3
                    )
                    chosen.append((rank, -volume, symbol))
            if reason is not None:
                self.exclusions[symbol] = reason
        return [symbol for *_, symbol in sorted(chosen)]

    async def seed(self, symbol: str) -> None:
        """Volatility/volume baseline from the last 60 CLOSED 1m candles (REST)."""
        now = datetime.now(UTC)
        bars = await self.bingx.range_candles(
            symbol, "1m", now - timedelta(minutes=61), now, limit=70
        )
        self.trackers[symbol].seed(
            [
                (int(b.open_time.timestamp() * 1000), float(b.close), float(b.volume))
                for b in bars
            ]
        )

    # ---- stream path (synchronous, per message) -------------------------------------

    def on_kline(
        self,
        bingx_symbol: str,
        open_ms: int,
        close: float,
        volume: float,
        received: float,
        high: float | None = None,
        low: float | None = None,
        open_: float | None = None,
    ) -> None:
        try:
            symbol = bingx_to_internal_symbol(bingx_symbol)
        except ValueError:
            return
        tracker = self.trackers.get(symbol)
        if tracker is None:
            return
        self.counters["fast_events_received"] += 1
        tracker.on_kline(open_ms, close, volume, received, high, low, open_)
        trigger = evaluate(tracker, received, self.config)
        if trigger is not None:
            self._on_fast(symbol, trigger, received)
        # Structural early events: at most one evaluation per symbol per second.
        if self.early is not None and received - self._last_early.get(symbol, 0.0) >= 1:
            self._last_early[symbol] = received
            try:
                events = self.early.on_tick(symbol, tracker, received)
            except Exception as exc:  # noqa: BLE001 - one symbol never stops the stream
                self.counters["early_errors:" + type(exc).__name__] += 1
                return
            for event in events:
                self._enqueue_early(event, received)

    def _on_fast(self, symbol: str, trigger: Trigger, received: float) -> None:
        decision = self.antispam.decide(symbol, trigger, received)
        if decision is None:
            self.counters["fast_events_suppressed"] += 1
            return
        event_type = EventType.FAST_MOVE
        extra: list[str] = []
        metrics: dict[str, Any] = {}
        if self.early is not None:
            event_type, extra, metrics = self.early.classify_fast(
                symbol, trigger, received
            )
            if trigger.state != FastState.FAST_MOVE:
                self.early.note_fast(symbol, trigger.direction, received)
        if event_type == EventType.LATE_EXTENDED_MOVE:
            if trigger.state == FastState.EXTREME_MOVE:
                event_type = EventType.EXTREME_MOVE  # extremes are reported once, as is
            else:
                self._late_fast(symbol, trigger, received, extra, metrics)
                return
        elif event_type == EventType.FAST_MOVE and trigger.state != FastState.FAST_MOVE:
            event_type = (
                EventType.MOMENTUM_CONFIRMED
                if trigger.state == FastState.CONFIRMED_MOMENTUM
                else EventType.EXTREME_MOVE
            )
        detected = self.clock()
        self.counters["fast_events_detected"] += 1
        if trigger.state == FastState.CONFIRMED_MOMENTUM:
            self.counters["fast_confirmed"] += 1
        if trigger.state == FastState.EXTREME_MOVE:
            self.counters["fast_extreme"] += 1
        self.latency["fast_detection_ms"].add(round((detected - received) * 1000, 2))
        job = FastJob(
            symbol,
            trigger,
            decision,
            received,
            detected,
            event_type=event_type.value,
            extra_reasons=extra,
            metrics=metrics,
        )
        self.policy.count(event_type.value, "detected")
        if not self.outbox.put_nowait(PRIORITY[event_type], job):
            self.counters["fast_events_dropped"] += 1
        self.request_rescan(symbol)

    def _late_fast(
        self, symbol: str, trigger: Trigger, received: float, extra, metrics
    ) -> None:
        """A fast move far from its origin: one LATE_EXTENDED_MOVE, never 'early'."""
        assert self.early is not None
        event = EarlyEvent(
            symbol=symbol,
            event_type=EventType.LATE_EXTENDED_MOVE,
            direction=trigger.direction,
            price=trigger.price,
            detected_at=received,
            reasons=[f"LATE_AFTER_{metrics.get('original_event', 'FAST_MOVE')}"]
            + trigger.reasons
            + extra,
            metrics={
                **metrics,
                "fast_change_pct": trigger.change_pct,
                "fast_window": trigger.window,
                "volume_ratio": trigger.volume_ratio,
            },
        )
        decision = self.early.antispam.decide(event, received)
        if decision is None:
            self.counters["fast_events_suppressed"] += 1
            return
        event.decision = decision
        self.counters["late_extended_events"] += 1
        self._enqueue_early(event, received)

    def _enqueue_early(self, event: EarlyEvent, received: float | None) -> None:
        detected = self.clock()
        self.counters["early_events_detected"] += 1
        self.counters[event.event_type.value.lower() + "_events"] += 1
        if received is not None:
            self.latency["fast_detection_ms"].add(
                round((detected - received) * 1000, 2)
            )
        self.policy.count(event.event_type.value, "detected")
        job = EarlyJob(event, received if received is not None else detected, detected)
        if not self.outbox.put_nowait(event.priority, job):
            self.counters["fast_events_dropped"] += 1
        if event.event_type in RESCAN_EVENTS:
            self.request_rescan(event.symbol)

    def request_rescan(self, symbol: str) -> None:
        """Closed-candle rescan of ONE symbol; duplicates while pending are merged
        and the rate is capped (never a full-market rescan)."""
        if self.rescan_symbol is None or symbol in self._pending_rescans:
            return
        now = time.monotonic()
        while self._rescan_times and now - self._rescan_times[0] > 60:
            self._rescan_times.popleft()
        limit = self.settings.early_priority_analyses_per_minute
        if len(self._rescan_times) >= limit:
            self.counters["fast_priority_analyses_rate_limited"] += 1
            return
        try:
            self.rescans.put_nowait(symbol)
            self._pending_rescans.add(symbol)
            self._rescan_times.append(now)
        except asyncio.QueueFull:
            self.counters["fast_rescans_dropped"] += 1

    # ---- context and dispatch ------------------------------------------------------

    def _db_context(
        self, symbol: str, direction: str
    ) -> tuple[FastContext, int | None, object, float | None]:
        context = FastContext(alert_score=self.settings.scanner_alert_score)
        reply_to, setup_id, oi_base = None, None, None
        with self.sessions() as session:
            snapshot = session.scalars(
                select(ScannerSnapshot)
                .where(
                    ScannerSnapshot.symbol == symbol,
                    ScannerSnapshot.exchange == "BINGX",
                )
                .order_by(ScannerSnapshot.created_at.desc())
                .limit(1)
            ).first()
            if snapshot is not None:
                context.technical_score = (
                    snapshot.long_score if direction == "LONG" else snapshot.short_score
                )
                for setup in snapshot.data.get("setups", []):
                    if setup.get("direction") == direction:
                        context.technical_state = setup.get("state")
            setups = session.scalars(
                select(MarketSetup).where(
                    MarketSetup.symbol == symbol, MarketSetup.lifecycle == "ACTIVE"
                )
            ).all()
            if setups:
                setup = next((s for s in setups if s.direction == direction), setups[0])
                context.active_setup = (
                    f"{setup.direction} {setup.score}/100 · {setup.state}"
                )
                reply_to = (setup.notified_data or {}).get("telegram_message_id")
                setup_id = setup.id
            try:
                from app.news.repository import setup_news_context

                news = setup_news_context(session, symbol, datetime.now(UTC), limit=1)
                if news and news[0].level in ("HIGH", "CRITICAL"):
                    age = int((datetime.now(UTC) - news[0].received_at).total_seconds())
                    context.news = f"{news[0].level} IMPACT NEWS {age} сек назад: {news[0].title[:120]}"
            except Exception:  # noqa: BLE001 - absent news never blocks a fast alert
                session.rollback()
            snapshot_oi = session.scalars(
                select(OpenInterestSnapshot)
                .where(
                    OpenInterestSnapshot.exchange == "BINGX",
                    OpenInterestSnapshot.symbol == symbol,
                    OpenInterestSnapshot.bucket_at
                    >= datetime.now(UTC) - timedelta(minutes=20),
                )
                .order_by(OpenInterestSnapshot.bucket_at.desc())
                .limit(1)
            ).first()
            if snapshot_oi is not None:
                context.oi_since = _utc(snapshot_oi.bucket_at).strftime("%H:%M")
                oi_base = float(snapshot_oi.open_interest)
        return context, reply_to, setup_id, oi_base

    async def _oi_change(
        self, symbol: str, price: float, base: float | None
    ) -> float | None:
        """Current OI vs the last stored boundary snapshot; one REST call, 0.8 s budget."""
        if base is None or base <= 0 or price <= 0:
            return None
        try:
            notional = await asyncio.wait_for(
                self.bingx.open_interest_notional(symbol), timeout=0.8
            )
            current = float(notional) / price
            return round((current / base - 1) * 100, 2)
        except Exception:  # noqa: BLE001 - OI is optional context ("нет данных")
            self.counters["provider_errors"] += 1
            return None

    async def _send(
        self, symbol: str, event: str, text: str, reply_to: int | None, started: float
    ) -> tuple[list[int], str | None]:
        """Deliver once; success means Telegram returned a real message_id."""
        if self.sender is None or not self.settings.fast_send_telegram:
            return [], None
        try:
            ids = await asyncio.wait_for(
                self.sender(
                    text, reply_to, [("📈 TradingView", tradingview_url(symbol))]
                ),
                timeout=self.settings.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never retried in a loop
            logger.warning(
                "Telegram delivery failed symbol=%s event=%s chat_fingerprint=%s error=%s",
                symbol,
                event,
                self.destination,
                type(exc).__name__,
            )
            return [], type(exc).__name__
        if ids:
            logger.info(
                "Telegram accepted symbol=%s event=%s message_id=%s chat_fingerprint=%s latency_ms=%.0f",
                symbol,
                event,
                ids[0],
                self.destination,
                (self.clock() - started) * 1000,
            )
        return ids, None

    def _analysis_latency(self, symbol: str, since: float) -> dict[str, float]:
        timing = self.analysis_times.get(symbol) or {}
        if timing.get("started", 0) < since - 1:
            return {}
        result = {"priority_analysis_started_at": timing["started"]}
        if "completed" in timing:
            result["priority_analysis_completed_at"] = timing["completed"]
            result["priority_analysis_ms"] = round(
                (timing["completed"] - timing["started"]) * 1000, 1
            )
        return result

    async def dispatch(self, job: FastJob | EarlyJob) -> None:
        if isinstance(job, EarlyJob):
            await self.dispatch_early(job)
            return
        job.stages["telegram_started"] = self.clock()
        context, reply_to, setup_id, oi_base = await asyncio.to_thread(
            self._db_context, job.symbol, job.trigger.direction
        )
        decision = self.policy.decide_fast(
            job.symbol,
            job.trigger.direction,
            job.trigger.state.value,
            job.decision,
            job.trigger.reasons + job.extra_reasons,
            job.detected,
            active_setup=context.active_setup is not None,
            near_htf=self._near_htf(job.symbol, job.trigger.price),
            setup_reply=reply_to,
        )
        ids: list[int] = []
        error = None
        if decision.send:
            context.oi_change_pct = await self._oi_change(
                job.symbol, job.trigger.price, oi_base
            )
            if context.oi_change_pct is None:
                context.oi_since = None
            text = format_fast_event(
                job.symbol,
                job.trigger,
                job.decision,
                context,
                datetime.fromtimestamp(job.detected, UTC),
                self.mode,
            )
            ids, error = await self._send(
                job.symbol, job.event_type, text, decision.reply_to, job.detected
            )
            if ids:
                self.policy.record_sent(
                    None,
                    job.symbol,
                    job.trigger.direction,
                    job.event_type,
                    ids[0],
                    job.detected,
                )
        self._count_decision(job.event_type, decision, ids)
        done = self.clock()
        latency = {
            "market_event_received_at": job.received,
            "fast_event_detected_at": job.detected,
            "telegram_started_at": job.stages["telegram_started"],
            "telegram_completed_at": done,
            "detection_ms": round((job.detected - job.received) * 1000, 2),
            "detected_to_telegram_ms": round((done - job.detected) * 1000, 1),
            "total_ms": round((done - job.received) * 1000, 1),
            **self._analysis_latency(job.symbol, job.detected),
        }
        if ids:
            self.counters["fast_events_sent"] += 1
            self.latency["fast_to_telegram_ms"].add(latency["total_ms"])
        await asyncio.to_thread(
            self._record, job, context, setup_id, ids, error, latency, decision
        )

    async def dispatch_early(self, job: EarlyJob) -> None:
        event = job.event
        started = self.clock()
        fast, reply_to, setup_id, oi_base = await asyncio.to_thread(
            self._db_context, event.symbol, event.direction
        )
        engine_ctx = self.early.contexts.get(event.symbol) if self.early else None
        context = EarlyContext(
            technical_score=fast.technical_score,
            technical_state=fast.technical_state,
            active_setup=fast.active_setup,
            news=fast.news,
            alert_score=self.settings.scanner_alert_score,
            funding_rate=engine_ctx.funding_rate if engine_ctx else None,
            funding_state=engine_ctx.funding_state if engine_ctx else None,
            market=dict(engine_ctx.market) if engine_ctx else {},
        )
        decision = self.policy.decide_early(event, job.detected, setup_reply=reply_to)
        ids: list[int] = []
        error = None
        if decision.send and self.settings.early_send_telegram:
            context.oi_change_pct = await self._oi_change(
                event.symbol, event.price, oi_base
            )
            context.oi_since = (
                fast.oi_since if context.oi_change_pct is not None else None
            )
            text = format_early_event(
                event, context, datetime.fromtimestamp(job.detected, UTC), self.mode
            )
            ids, error = await self._send(
                event.symbol,
                event.event_type.value,
                text,
                decision.reply_to,
                job.detected,
            )
            if ids:
                self.policy.record_sent(
                    event,
                    event.symbol,
                    event.direction,
                    event.event_type.value,
                    ids[0],
                    job.detected,
                    decision.episode_key,
                )
        self._count_decision(event.event_type.value, decision, ids)
        done = self.clock()
        latency = {
            "market_event_received_at": job.received,
            "fast_event_detected_at": job.detected,
            "telegram_started_at": started,
            "telegram_completed_at": done,
            "detection_ms": round((job.detected - job.received) * 1000, 2),
            "detected_to_telegram_ms": round((done - job.detected) * 1000, 1),
            "total_ms": round((done - job.received) * 1000, 1),
            **self._analysis_latency(event.symbol, job.detected),
        }
        if ids:
            self.counters["fast_events_sent"] += 1
            self.latency["fast_to_telegram_ms"].add(latency["detected_to_telegram_ms"])
        await asyncio.to_thread(
            self._record_early, job, context, setup_id, ids, error, latency, decision
        )

    def _record(self, job, context, setup_id, ids, error, latency, decision) -> None:
        t = job.trigger
        with self.sessions() as session, session.begin():
            session.add(
                FastMarketEvent(
                    exchange="BINGX",
                    symbol=job.symbol,
                    direction=t.direction,
                    state=t.state.value,
                    decision=job.decision,
                    window=t.window,
                    change_pct=t.change_pct,
                    threshold_pct=t.threshold_pct,
                    sigma_pct=t.sigma_pct,
                    volume_ratio=t.volume_ratio,
                    reasons=t.reasons + job.extra_reasons,
                    price=t.price,
                    technical_score=context.technical_score,
                    setup_state=context.technical_state,
                    market_setup_id=setup_id,
                    context={
                        "oi_change_pct": context.oi_change_pct,
                        "news": context.news,
                        "active_setup": context.active_setup,
                        "mode": self.mode,
                        "notify": _notify(decision),
                    },
                    received_at=datetime.fromtimestamp(job.received, UTC),
                    detected_at=datetime.fromtimestamp(job.detected, UTC),
                    sent=bool(ids),
                    telegram_message_id=ids[0] if ids else None,
                    error=error,
                    latency_ms=latency,
                    event_type=job.event_type,
                    metrics=job.metrics or None,
                )
            )
        self.policy.count(job.event_type, "persisted")

    def _record_early(
        self, job, context, setup_id, ids, error, latency, decision
    ) -> None:
        event: EarlyEvent = job.event
        detected = datetime.fromtimestamp(job.detected, UTC)
        with self.sessions() as session, session.begin():
            if event.episode is not None:
                early_service.save_episode(session, event.episode)
                session.flush()  # the event row references the episode
            if (
                event.pattern is not None
                and event.event_type == EventType.FORMATION_WATCH
            ):
                early_service.save_pattern(
                    session, event.symbol, event.pattern, "ACTIVE", detected
                )
            session.add(
                FastMarketEvent(
                    exchange="BINGX",
                    symbol=event.symbol,
                    direction=event.direction,
                    state=event.event_type.value,
                    decision=event.decision,
                    volume_ratio=event.metrics.get("volume_ratio"),
                    reasons=list(event.reasons),
                    price=event.price,
                    technical_score=context.technical_score,
                    setup_state=context.technical_state,
                    market_setup_id=setup_id,
                    context={
                        "key": event.key,
                        "confirmations": list(event.confirmations),
                        "oi_change_pct": context.oi_change_pct,
                        "news": context.news,
                        "active_setup": context.active_setup,
                        "market": context.market,
                        "funding_rate": context.funding_rate,
                        "mode": self.mode,
                        "notify": _notify(decision),
                    },
                    received_at=datetime.fromtimestamp(job.received, UTC),
                    detected_at=detected,
                    sent=bool(ids),
                    telegram_message_id=ids[0] if ids else None,
                    error=error,
                    latency_ms=latency,
                    event_type=event.event_type.value,
                    strength=event.strength,
                    source_timeframe=event.source_timeframe,
                    zone=event.zone.as_dict() if event.zone else None,
                    metrics={**event.metrics, "invalidation": invalidation_for(event)},
                    episode_id=event.episode.id if event.episode else None,
                )
            )
        self.policy.count(event.event_type.value, "persisted")

    def _count_decision(self, event_type: str, decision: Decision, ids) -> None:
        if decision.send:
            self.policy.count(event_type, "telegram_eligible")
            if ids:
                self.policy.count(event_type, "sent")
        else:
            key = {
                "policy": "suppressed_by_policy",
                "duplicate": "suppressed_duplicate",
                "late": "suppressed_late",
                "rate_limit": "suppressed_rate_limit",
            }[decision.reason]
            self.policy.count(event_type, key)

    def _near_htf(self, symbol: str, price: float) -> bool:
        """Price at/near a 1H/4H zone of the latest closed-candle context."""
        ctx = self.early.contexts.get(symbol) if self.early else None
        if ctx is None:
            return False
        reach = self.policy.cfg.fast_near_htf_atr
        return any(
            z.htf and z.lower - reach * z.atr <= price <= z.upper + reach * z.atr
            for z in ctx.zones
        )

    def restore_cooldowns(self) -> None:
        """Recent events survive a restart: no duplicate alert for the same move,
        breakout episode or zone; stale state is never replayed as fresh."""
        horizon = max(
            self.config.cooldown_seconds,
            self.early_cfg.cooldown_minutes * 60,
            self.early_cfg.episode_max_hours * 3600,
        )
        since = datetime.now(UTC) - timedelta(seconds=horizon)
        fast_since = datetime.now(UTC) - timedelta(seconds=self.config.cooldown_seconds)
        with self.sessions() as session:
            rows = session.scalars(
                select(FastMarketEvent)
                .where(FastMarketEvent.detected_at >= since)
                .order_by(FastMarketEvent.detected_at)
            ).all()
            episodes = (
                early_service.load_open_episodes(
                    session, self.early_cfg.episode_max_hours
                )
                if self.early is not None
                else []
            )
        for row in rows:
            at = _utc(row.detected_at)
            if row.state in FAST_STATES and at >= fast_since:
                self.antispam.seed(
                    row.symbol,
                    row.direction,
                    FastState(row.state),
                    row.change_pct or 0.0,
                    at,
                )
            if self.early is not None and row.event_type:
                self.early.antispam.seed(
                    row.symbol,
                    row.direction,
                    row.event_type,
                    (row.context or {}).get("key", ""),
                    at.timestamp(),
                    row.strength,
                )
        thread_since = time.time() - self.policy.cfg.thread_hours * 3600
        for row in rows:
            at_epoch = _utc(row.detected_at).timestamp()
            if row.sent and at_epoch >= thread_since:
                self.policy.restore(
                    row.symbol,
                    row.direction,
                    row.event_type or row.state,
                    KeyZone.from_dict(row.zone) if row.zone else None,
                    str(row.episode_id) if row.episode_id else None,
                    row.telegram_message_id,
                    at_epoch,
                )
        for episode in episodes:
            assert self.early is not None
            self.early.restore_episode(episode)
        self.counters["restored_episodes"] = len(episodes)

    # ---- workers and lifecycle -----------------------------------------------------

    async def _telegram_worker(self) -> None:
        while True:
            _, job = await self.outbox.get()
            try:
                await self.dispatch(job)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - one bad event never stops the watcher
                self.counters["fast_dispatch_errors"] += 1
                logger.warning(
                    "Fast event dispatch failed error=%s", type(exc).__name__
                )

    async def _rescan_worker(self) -> None:
        assert self.rescan_symbol is not None
        while True:
            symbol = await self.rescans.get()
            started = time.monotonic()
            self.analysis_times[symbol] = {"started": self.clock()}
            try:
                await self.rescan_symbol(symbol)
                self.counters["fast_priority_analyses"] += 1
                self.analysis_times[symbol]["completed"] = self.clock()
                self.latency["priority_analysis_ms"].add(
                    round((time.monotonic() - started) * 1000, 1)
                )
            except Exception as exc:  # noqa: BLE001 - scanner busy/failing never stops us
                self.counters["fast_rescans_failed:" + type(exc).__name__] += 1
            finally:
                self._pending_rescans.discard(symbol)

    async def refresh_contexts(self, symbols: list[str] | None = None) -> int:
        """One closed-candle context pass (every 5m close). Returns contexts built."""
        if self.early is None:
            return 0
        now = datetime.now(UTC)
        chosen = symbols or list(self.trackers)
        majors = await self.contexts.majors(now)
        semaphore = asyncio.Semaphore(self.settings.early_context_concurrency)
        built = 0
        candidates: list[EarlyEvent] = []

        async def one(symbol: str) -> None:
            nonlocal built
            async with semaphore:
                try:
                    ctx = await self.contexts.build(
                        symbol, now, majors, self.spreads.get(symbol)
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one market
                    self.counters["provider_errors"] += 1
                    self.counters["context_errors:" + type(exc).__name__] += 1
                    return
            assert self.early is not None
            tracker = self.trackers.get(symbol)
            price = tracker.samples[-1][1] if tracker and tracker.samples else None
            candidates.extend(
                self.early.set_context(
                    ctx,
                    price,
                    self.clock(),
                    prime=symbol not in self._primed,
                    defer=True,
                )
            )
            self._primed.add(symbol)
            built += 1

        await asyncio.gather(*(one(s) for s in chosen))
        for event in self.early.gate_pass(candidates, self.clock()):
            self._enqueue_early(event, None)
        await asyncio.to_thread(self.flush_engine_state)
        self.counters["context_passes"] += 1
        return built

    def flush_engine_state(self) -> None:
        """Persist cooled-down / progressing episodes and pattern status changes."""
        if self.early is None:
            return
        finished, self.early.finished = self.early.finished, []
        statuses, self.early.pattern_status = self.early.pattern_status, []
        now = datetime.now(UTC)
        with self.sessions() as session, session.begin():
            for episode in finished:
                early_service.save_episode(session, episode)
            for episodes in self.early.episodes.values():
                for episode in episodes.values():
                    early_service.save_episode(session, episode)
            for symbol, pattern, status in statuses:
                early_service.save_pattern(session, symbol, pattern, status, now)

    async def _context_loop(self) -> None:
        while True:
            try:
                await self.refresh_contexts()
            except Exception as exc:  # noqa: BLE001 - never stops the watcher
                self.counters["context_pass_errors:" + type(exc).__name__] += 1
                logger.warning("Early context pass failed error=%s", type(exc).__name__)
            now = time.time()
            # 35 s after the next 5m close: past the provider's 30 s server-time
            # cache, otherwise the new closed candle is not yet visible and the
            # closed-candle staleness guard rejects the whole pass (seen live).
            await asyncio.sleep(300 - now % 300 + 35)

    async def _outcome_loop(self) -> None:
        outcomes = early_service.EarlyOutcomeService(
            self.settings, self.sessions, self.bingx
        )
        while True:
            await asyncio.sleep(900)
            try:
                self.counters["early_outcomes_measured"] += await outcomes.run()
            except Exception as exc:  # noqa: BLE001 - research never stops the watcher
                self.counters["early_outcome_errors:" + type(exc).__name__] += 1

    async def start(self) -> list[str]:
        symbols = await self.universe()
        for symbol in symbols:
            self.trackers[symbol] = SymbolTracker(symbol)
        await asyncio.to_thread(self.restore_cooldowns)
        bingx_symbols = [internal_to_bingx_symbol(s) for s in symbols]
        size = self.settings.fast_symbols_per_connection
        self.connections = [
            StreamConnection(i, bingx_symbols[start : start + size], self.on_kline)
            for i, start in enumerate(range(0, len(bingx_symbols), size))
        ]
        self._tasks = [
            *(asyncio.create_task(c.run(self._stop)) for c in self.connections),
            asyncio.create_task(self._telegram_worker()),
            asyncio.create_task(self._seed_all(symbols)),
        ]
        if self.early is not None:
            self._tasks += [
                asyncio.create_task(self._context_loop()),
                asyncio.create_task(self._outcome_loop()),
            ]
        if self.rescan_symbol is not None:
            self._tasks.append(asyncio.create_task(self._rescan_worker()))
        logger.info(
            "Fast universe selected=%s excluded=%s",
            len(symbols),
            dict(Counter(self.exclusions.values())),
        )
        return symbols

    async def _seed_all(self, symbols: list[str]) -> None:
        # Through the shared BingX limiter; symbols arm as their baseline arrives.
        for symbol in symbols:
            try:
                await self.seed(symbol)
            except Exception as exc:  # noqa: BLE001 - stream baseline will fill in
                self.counters["fast_seed_errors"] += 1
                self.counters["provider_errors"] += 1
                logger.debug(
                    "Fast baseline seed failed %s %s", symbol, type(exc).__name__
                )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.early is not None:
            try:
                await asyncio.to_thread(self.flush_engine_state)
            except Exception as exc:  # noqa: BLE001 - shutdown must complete
                logger.warning("Early state flush failed error=%s", type(exc).__name__)

    def snapshot(self) -> dict:
        ages = [age for c in self.connections if (age := c.age_ms()) is not None]
        detection = self.latency["fast_detection_ms"]
        telegram = self.latency["fast_to_telegram_ms"]
        early = self.early
        return {
            "symbols_subscribed": sum(len(c.symbols) for c in self.connections),
            "connections": len(self.connections),
            "transport": "WEBSOCKET kline_1m (multiplexed)",
            "armed_symbols": sum(
                len(t.closed) >= self.config.min_baseline_minutes
                for t in self.trackers.values()
            ),
            "early_contexts": len(early.contexts) if early else 0,
            "open_episodes": sum(len(e) for e in early.episodes.values())
            if early
            else 0,
            "counters": dict(self.counters),
            "early_engine": dict(early.counters) if early else {},
            "early_antispam": dict(early.antispam.counters) if early else {},
            "universe_excluded": dict(Counter(self.exclusions.values())),
            # detected / persisted / telegram_eligible / sent / suppressed_* by type
            "notification": self.policy.snapshot(),
            # the SAME event seen again on later ticks (not new events, not stored)
            "redetections": {
                **(dict(early.antispam.duplicates) if early else {}),
                "FAST(any)": self.counters["fast_events_suppressed"],
            },
            "fast_queue_depth": self.outbox.qsize(),
            "fast_events_dropped": self.counters["fast_events_dropped"],
            "fast_priority_analyses": self.counters["fast_priority_analyses"],
            "websocket_reconnects": sum(c.reconnects for c in self.connections),
            "stream_messages": sum(c.messages for c in self.connections),
            "stream_age_ms": max(ages) if ages else None,
            "provider_errors": self.counters["provider_errors"],
            "frames_reused_from_scanner": self.contexts.frames_reused,
            "frames_computed": self.contexts.frames_computed,
            "fast_detection_p50_ms": detection.percentile(50),
            "fast_detection_p95_ms": detection.percentile(95),
            "fast_detection_p99_ms": detection.percentile(99),
            "fast_telegram_p50_ms": telegram.percentile(50),
            "fast_telegram_p95_ms": telegram.percentile(95),
            "latency": {k: v.summary() for k, v in self.latency.items()},
        }
