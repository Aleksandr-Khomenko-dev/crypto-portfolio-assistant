"""Worker for stored TradingView webhooks (runs inside the scanner runner).

new raw_webhook row -> format (ICT v4.2 / legacy) -> filters -> trade + event
(unique planId + event) -> owner-only Telegram entry message -> delivery record.

Delivery is at-most-once: the event is committed before sending, so a crash can
lose a message but never duplicate one. Follow-ups (TP1, STOP, ...) are stored
and summed into the trade's R; their threaded replies come in the next step.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.scanner_models import Delivery, RawWebhook, Trade, TradeEvent
from app.macro.calendar import EconomicCalendar
from app.scanner.domain import Readiness, ScannerResult
from app.telegram.tv_ict import EntryContext, format_entry, news_window
from app.tv.ict import (
    CLOSING,
    FOLLOW_UPS,
    KNOWN_EVENTS,
    IctSignal,
    candle_close,
    check_levels,
    decode,
    detect_format,
    late_minutes,
    parse,
    symbol_allowed,
    validation_error,
)

logger = logging.getLogger(__name__)
# (chat_id, text, reply_to, buttons) -> Telegram message ids (empty = not sent)
Sender = Callable[
    [str, str, int | None, list[tuple[str, str]] | None], Awaitable[list[int]]
]
SCANNER_STALE = timedelta(hours=1)


@dataclass
class Outgoing:
    chat_id: str
    text: str
    trade_event_id: int | None = None  # None: legacy forward (no trade record)
    audience: str = "owner"


def scanner_view(
    direction: str, result: ScannerResult | None, settings: Settings, now: datetime
) -> str:
    """The bot's own closed-candle scanner on the same market, in one word."""
    if result is None:
        return "нет данных"
    created = result.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if now - created > SCANNER_STALE:
        return "нет данных"
    same = next((s for s in result.setups if s.direction == direction), None)
    other = next((s for s in result.setups if s.direction != direction), None)
    if same is None:
        return "нет данных"
    if same.readiness == Readiness.WAIT_MACRO or (
        other is not None and other.score >= same.score + 10
    ):
        return "против"
    if same.readiness == Readiness.READY or same.score >= settings.scanner_setup_score:
        return "совпадает"
    return "нейтрально"


class TradingViewWorker:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        sender: Sender | None,
        calendar: EconomicCalendar | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings, self.sessions, self.sender = settings, sessions, sender
        self.calendar = calendar
        self.clock = clock

    # ---- loop ---------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            handled = 0
            try:
                handled = await self.process_pending()
            except Exception as exc:  # noqa: BLE001 - the worker never dies
                logger.warning(
                    "TradingView worker pass failed error=%s", type(exc).__name__
                )
            if not handled:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.settings.tv_worker_poll_seconds
                    )
                except TimeoutError:
                    pass

    async def process_pending(self, limit: int = 20) -> int:
        ids = await asyncio.to_thread(self._pending_ids, limit)
        for row_id in ids:
            try:
                outgoing = await asyncio.to_thread(self._handle, row_id)
            except Exception as exc:  # noqa: BLE001 - one bad row never blocks others
                logger.warning(
                    "TradingView row failed id=%s error=%s", row_id, type(exc).__name__
                )
                await asyncio.to_thread(self._mark, row_id, "error", type(exc).__name__)
                continue
            if outgoing is not None:
                await self._deliver(outgoing)
        return len(ids)

    def _pending_ids(self, limit: int) -> list[int]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(RawWebhook.id)
                    .where(RawWebhook.status == "new")
                    .order_by(RawWebhook.id)
                    .limit(limit)
                )
            )

    def _mark(self, row_id: int, status: str, error: str | None = None) -> None:
        with self.sessions() as session:
            row = session.get(RawWebhook, row_id)
            if row is not None:
                row.status, row.error = status, error
                row.processed_at = self.clock()
                session.commit()

    # ---- one row --------------------------------------------------------------------

    def _handle(self, row_id: int) -> Outgoing | None:
        with self.sessions() as session:
            row = session.get(RawWebhook, row_id)
            if row is None or row.status != "new":
                return None
            row.processed_at = self.clock()
            # SQLite returns naive datetimes; they are stored in UTC.
            if row.received_at.tzinfo is None:
                row.received_at = row.received_at.replace(tzinfo=UTC)
            fmt = detect_format(
                decode(row.body), self.settings.legacy_tv_format_enabled
            )
            if fmt is None:
                self._finish(session, row, "invalid", "not JSON or unknown format")
                return None
            row.format = fmt
            if fmt == "legacy":
                return self._legacy(session, row)
            data = decode(row.body, exact=True)
            assert data is not None
            try:
                signal = parse(data)
            except ValidationError as error:
                self._finish(session, row, "invalid", validation_error(error))
                return None
            row.plan_id, row.event = signal.plan_id, signal.event
            if not symbol_allowed(signal.ticker, self.settings.tv_allowed_symbols):
                self._finish(session, row, "ignored", "symbol not allowed")
                return None
            if signal.event not in KNOWN_EVENTS:
                self._finish(session, row, "ignored", "unknown event")
                return None
            if session.scalar(
                select(TradeEvent.id).where(
                    TradeEvent.plan_id == signal.plan_id,
                    TradeEvent.event == signal.event,
                )
            ):
                self._finish(session, row, "duplicate", None)
                return None
            try:
                outgoing = self._apply(session, row, signal, decode(row.body) or {})
                row.status = "processed"
                session.commit()
            except IntegrityError:  # a concurrent copy won the unique key
                session.rollback()
                self._mark(row_id, "duplicate")
                return None
            return outgoing

    def _finish(
        self, session: Session, row: RawWebhook, status: str, error: str | None
    ) -> None:
        row.status, row.error = status, error
        session.commit()
        if status in ("invalid", "error"):
            logger.warning(
                "TradingView webhook %s id=%s reason=%s", status, row.id, error
            )

    def _legacy(self, session: Session, row: RawWebhook) -> Outgoing | None:
        from fastapi import HTTPException

        from app.api.routers.tradingview import TradingViewPayload, handle_legacy

        if not row.secret_ok:
            self._finish(session, row, "invalid", "legacy secret missing or wrong")
            return None
        try:
            payload = TradingViewPayload.model_validate_json(row.body)
            _, text = handle_legacy(session, payload, self.settings, row.received_at)
        except ValidationError as error:
            self._finish(session, row, "invalid", validation_error(error))
            return None
        except HTTPException as error:
            self._finish(session, row, "invalid", str(error.detail))
            return None
        row.plan_id, row.event = payload.event_id[:120], payload.event
        self._finish(session, row, "processed", None)
        chat = self.settings.scanner_telegram_chat_id
        return Outgoing(chat, text) if text and chat else None

    def _apply(
        self, session: Session, row: RawWebhook, signal: IctSignal, raw: dict
    ) -> Outgoing | None:
        now = self.clock()
        trade = session.scalar(select(Trade).where(Trade.plan_id == signal.plan_id))
        if trade is None:
            trade = Trade(
                plan_id=signal.plan_id,
                source="tv_ict_v42",
                ticker=signal.ticker,
                side=signal.side,
                status="WAITING",
                result_r=0.0,
                created_at=now,
                updated_at=now,
            )
            session.add(trade)
        self._copy_plan(trade, signal)
        trade.updated_at = now
        event = TradeEvent(
            plan_id=signal.plan_id,
            event=signal.event,
            raw_webhook_id=row.id,
            price=signal.price,
            exit_price=signal.exit_price,
            result_r=signal.result_r,
            bar_at=signal.time(signal.bar_time),
            received_at=row.received_at,
            notified=False,
            payload=raw,
        )
        session.flush()  # trade.id
        event.trade_id = trade.id
        session.add(event)

        outgoing = None
        if signal.event == "ZONE_WAIT":
            event.notify_reason = "zone: stored only (/watch)"
        elif signal.event == "CANCELLED":
            trade.status, trade.closed_at = "CANCELLED", now
            event.notify_reason = "zone cancelled: stored only"
        elif signal.event == "ENTRY":
            outgoing = self._entry(session, trade, event, signal, row.received_at)
        elif signal.event == "PLAN_NEW":
            event.notify_reason = "limit mode: delivered in the next step"
        elif signal.event in FOLLOW_UPS:
            if signal.result_r is not None:
                trade.result_r = (trade.result_r or 0.0) + signal.result_r
            if signal.event in CLOSING:
                trade.status, trade.closed_at = "CLOSED", now
            event.notify_reason = "follow-up: threaded replies in the next step"
        session.flush()
        if outgoing is not None:
            outgoing.trade_event_id = event.id
        return outgoing

    @staticmethod
    def _copy_plan(trade: Trade, signal: IctSignal) -> None:
        """Plan fields from the indicator; a later event never erases known values."""
        values = {
            "symbol": signal.symbol,
            "timeframe": signal.tf,
            "model": signal.model,
            "score": signal.score,
            "entry_type": signal.entry_type,
            "entry": signal.entry,
            "sl": signal.sl,
            "tp1": signal.tp1,
            "tp2": signal.tp2,
            "tp3": signal.tp3,
            "tp2_name": signal.tp2_name,
            "tp3_name": signal.tp3_name,
            "units": signal.units,
            "risk_usd": signal.risk_usd,
            "leverage": signal.leverage,
            "window": signal.window,
            "checklist": signal.checklist,
            "bias_main": signal.bias_main,
            "bias_4h": signal.bias_4h,
            "day_range_pct": signal.day_range_pct,
            "valid_hours": signal.valid_hours,
            "born_at": signal.time(signal.born_time),
        }
        for name, value in values.items():
            if value is not None:
                setattr(trade, name, value)

    def _entry(
        self,
        session: Session,
        trade: Trade,
        event: TradeEvent,
        signal: IctSignal,
        received_at: datetime,
    ) -> Outgoing | None:
        settings = self.settings
        problem = check_levels(signal)
        if problem is not None:
            trade.status, trade.notify_reason = "INVALID", problem[:60]
            event.notify_reason = "invalid levels"
            logger.warning(
                "TradingView ENTRY rejected plan=%s reason=%s", signal.plan_id, problem
            )
            return None
        trade.entry_at = candle_close(signal)
        late = late_minutes(signal, received_at)
        trade.late_minutes = late
        context = self._context(session, signal, received_at)
        trade.scanner_agreement = context.scanner
        trade.news_nearby = bool(context.news_now)
        if late is not None and late > settings.tv_late_drop_minutes:
            trade.status = trade.notify_reason = "LATE"
            event.notify_reason = f"late by {late:.0f} min"
            return None
        if signal.score is not None and signal.score < settings.tv_min_score_to_notify:
            trade.status, trade.notify_reason = "FILTERED", "score below minimum"
            event.notify_reason = trade.notify_reason
            return None
        trade.status = "OPEN"
        chat = settings.owner_chat
        if not (chat and settings.telegram_bot_token and self.sender is not None):
            event.notify_reason = "telegram not configured"
            return None
        text = format_entry(signal, context, settings, self.clock())
        return Outgoing(chat, text)

    def _context(
        self, session: Session, signal: IctSignal, received_at: datetime
    ) -> EntryContext:
        from app.api.routers.tradingview import bot_symbol
        from app.scanner.repository import ScannerRepository

        now = self.clock()
        context = EntryContext(late_minutes=late_minutes(signal, received_at))
        try:
            results = ScannerRepository(session).latest_results(
                symbol=bot_symbol(signal.ticker, self.settings), limit=1
            )
            result = results[0] if results else None
            context.scanner = scanner_view(signal.direction, result, self.settings, now)
            if result is not None and result.context.kind == "GOLD_MACRO":
                context.macro = dict(result.context.assets)
        except Exception as exc:  # noqa: BLE001 - context is optional
            logger.debug("Scanner context unavailable error=%s", type(exc).__name__)
        calendar = self.calendar
        if calendar is None and self.settings.calendar_enabled:
            calendar = EconomicCalendar(self.settings)  # reads the runner's cache file
        if calendar is None or calendar.fetched_at is None:
            context.calendar_ok = False
        else:
            moment = candle_close(signal) or received_at
            events = calendar.events
            context.news_now = news_window(events, moment, self.settings)
            upcoming = [e for e in events if now <= e.at <= now + timedelta(hours=24)]
            context.next_event = upcoming[0] if upcoming else None
        return context

    # ---- Telegram ---------------------------------------------------------------------

    async def _deliver(self, outgoing: Outgoing) -> None:
        if self.sender is None:
            return
        ids: list[int] = []
        error = None
        try:
            ids = await asyncio.wait_for(
                self.sender(outgoing.chat_id, outgoing.text, None, None),
                timeout=self.settings.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never retried (no duplicates)
            error = type(exc).__name__
            logger.warning("TradingView Telegram delivery failed error=%s", error)
        if outgoing.trade_event_id is not None:
            await asyncio.to_thread(self._record, outgoing, ids, error)
        if ids:
            logger.info("TradingView entry delivered message_id=%s", ids[0])

    def _record(self, outgoing: Outgoing, ids: list[int], error: str | None) -> None:
        with self.sessions() as session:
            event = session.get(TradeEvent, outgoing.trade_event_id)
            if event is not None and ids:
                event.notified, event.notify_reason = True, None
            elif event is not None:
                event.notify_reason = f"telegram failed: {error or 'no message id'}"[
                    :60
                ]
            session.add(
                Delivery(
                    trade_event_id=outgoing.trade_event_id,
                    audience=outgoing.audience,
                    chat_id=outgoing.chat_id,
                    message_id=ids[0] if ids else None,
                    sent_at=self.clock(),
                    error=error,
                )
            )
            session.commit()
