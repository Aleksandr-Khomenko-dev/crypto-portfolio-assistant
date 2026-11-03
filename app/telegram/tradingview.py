"""Russian Telegram message for a TradingView SIGNAL webhook (QS PRO Gold).

Two independent systems side by side: the indicator's plan and the bot's own latest
closed-candle analysis of the same market. Agreement is reported, never averaged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from app.macro.calendar import EconomicEvent
from app.scanner.domain import Direction, Readiness, ScannerResult
from app.scanner.universe import display_symbol
from app.telegram.i18n import catalog

SEPARATOR = "━━━━━━━━━━━━━━━━━━"
STALE = timedelta(hours=1)


def _num(value: Decimal | None) -> str:
    return "—" if value is None else f"{value.normalize():f}"


def _until(then: datetime, now: datetime) -> str:
    minutes = max(0, int((then - now).total_seconds() // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"через {days} дн {hours} ч"
    if hours:
        return f"через {hours} ч {mins} мин"
    return f"через {mins} мин"


def agreement(direction: str, result: ScannerResult | None, settings) -> str:
    if result is None:
        return "⚪ Бот ещё не анализировал этот рынок — сравнить не с чем."
    same = next(s for s in result.setups if s.direction == direction)
    other = next(s for s in result.setups if s.direction != direction)
    if same.readiness == Readiness.READY:
        return "✅ Бот тоже видит готовый сетап в эту сторону."
    if same.readiness == Readiness.WAIT_EVENT:
        return "⏸ Бот в паузе из-за важных новостей — вход сейчас рискованнее обычного."
    if same.readiness == Readiness.WAIT_MACRO:
        return "⚠️ Бот блокирует это направление: макро-фон против сделки."
    if other.score >= same.score + 10:
        context = result.context
        blocked = context.kind == "GOLD_MACRO" and (
            (other.direction == Direction.SHORT and context.risk_off)
            or context.state == ("BULLISH" if direction == "LONG" else "BEARISH")
        )
        return (
            "⚠️ Бот сильнее видит ПРОТИВОПОЛОЖНОЕ направление, но макро-фон против него."
            if blocked
            else "⚠️ Бот сильнее видит ПРОТИВОПОЛОЖНОЕ направление."
        )
    if same.score >= settings.scanner_setup_score:
        return "🟡 Бот видит формирующийся сетап в ту же сторону, но не все условия выполнены."
    return "⚪ Бот пока не подтверждает этот сигнал."


def format_tradingview_signal(
    payload: Any,  # TradingViewPayload with event == "SIGNAL"
    bot_symbol: str,
    result: ScannerResult | None,
    next_event: EconomicEvent | None,
    settings,
    now: datetime,
) -> str:
    t = catalog(settings.telegram_language)
    tz: ZoneInfo = settings.tzinfo
    direction = (
        payload.direction.value
        if hasattr(payload.direction, "value")
        else str(payload.direction)
    )
    icon = "🟢" if direction == "LONG" else "🔴"
    grade = "A+ " if payload.grade == "A+" else ""
    lines = [
        f"📈 <b>TRADINGVIEW: {grade}{direction}</b> · {escape(display_symbol(bot_symbol))}",
        SEPARATOR,
        f"🕐 {escape(payload.timeframe)} · {payload.timestamp.astimezone(tz):%d.%m %H:%M} ({escape(str(tz))})",
    ]
    if payload.score is not None:
        lines.append(
            f"⭐ {escape(payload.source or 'Индикатор')}: <b>{payload.score}/100</b>"
        )
    lines.append(
        f"{icon} Вход <code>{_num(payload.price)}</code> · SL <code>{_num(payload.stop)}</code>"
    )
    targets = [
        f"TP{i} <code>{_num(v)}</code>"
        for i, v in enumerate((payload.tp1, payload.tp2, payload.tp3), 1)
        if v is not None
    ]
    if targets:
        lines.append("🎯 " + " · ".join(targets))
    if payload.lots is not None:
        risk = (
            f" · риск ${_num(payload.risk_usd)}" if payload.risk_usd is not None else ""
        )
        lines.append(f"📦 Размер: {_num(payload.lots)} лот{risk}")
    if payload.reason:
        lines.append(f"🧩 Причины: {escape(payload.reason)}")
    lines += ["", "🤖 <b>Бот по этому рынку</b>"]
    if result is None:
        lines.append(t["na"])
    else:
        age = now - result.candle_closed_at
        lines.append(
            f"<i>Сканер, закрытие 15m {result.candle_closed_at.astimezone(tz):%H:%M}"
            + (" — данные устарели" if age > STALE else "")
            + "</i>"
        )
        for setup in result.setups:
            status = (
                t["ready"]
                if setup.readiness == Readiness.READY
                else t[f"readiness.{setup.readiness}"]
            )
            mark = "🟢" if setup.direction == Direction.LONG else "🔴"
            lines.append(
                f"{mark} {setup.direction.value} {setup.score}/100 · {escape(status)}"
            )
        context = result.context
        if context.kind == "GOLD_MACRO":
            vix = context.assets.get("VIX", {}).get("vix")
            lines.append(
                "🌍 Макро: "
                + t.get(f"macro.state.{context.state}", escape(context.state))
                + (f" · VIX {vix}" if vix is not None else "")
                + (" · " + t["macro.risk_off"] if context.risk_off else "")
            )
    if next_event is not None:
        lines.append(
            f"📅 Ближайшее событие: {escape(next_event.title)} · "
            f"{next_event.at.astimezone(tz):%d.%m %H:%M} ({_until(next_event.at, now)})"
        )
    lines += [
        "",
        agreement(direction, result, settings),
        SEPARATOR,
        (
            "<i>Сигнал индикатора и анализ бота — информация для решения, "
            "не торговая рекомендация. Автоматической торговли нет.</i>"
        ),
    ]
    return "\n".join(lines)
