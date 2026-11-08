"""Russian Telegram message for a confirmed ICT v4.2 entry (owner only).

The indicator decides the entry, stop and targets; the bot only lays them out,
computes R multiples and adds context (calendar, macro, its own scanner's view).
Dynamic values are HTML-escaped; missing data is shown as such, never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any

from app.config import Settings
from app.macro.calendar import EconomicEvent
from app.telegram.tradingview import _until
from app.tv.ict import IctSignal, candle_close, lookup, timeframe_label

ARROW = {"UP": "↑", "DOWN": "↓", "FLAT": "→"}
CITY = {"Europe/Brussels": "по Брюсселю"}


@dataclass
class EntryContext:
    """Everything known when the entry is sent; None = нет данных."""

    late_minutes: float | None = None
    news_now: list[EconomicEvent] = field(default_factory=list)  # inside the window
    next_event: EconomicEvent | None = None
    calendar_ok: bool = True
    macro: dict[str, Any] = field(default_factory=dict)  # GOLD_MACRO assets
    scanner: str = "нет данных"  # совпадает | против | нейтрально | нет данных


def decimals(signal: IctSignal, settings: Settings) -> int:
    """Price step: configured per ticker, else the finest precision the indicator
    sent (at least 2), so 3969.8 shows as 3969.80."""
    configured = lookup(settings.tv_price_decimals, signal.ticker)
    if configured is not None and configured.isdigit():
        return int(configured)
    seen = []
    for value in (signal.entry, signal.sl, signal.tp1, signal.tp2, signal.tp3):
        exponent = value.as_tuple().exponent if value is not None else 0
        if isinstance(exponent, int):
            seen.append(-exponent)
    return min(8, max([2, *seen]))


def price(value: Decimal | None, places: int) -> str:
    if value is None:
        return "—"
    return f"{value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP):f}"


def r_label(r: float) -> str:
    """1R, 1.5R exactly; otherwise ≈2.5R."""
    half = round(r * 2) / 2
    if abs(r - half) < 0.02:
        return f"{half:g}R"
    return f"≈{r:.1f}R"


def money(value: Decimal) -> str:
    """$14 180 (thousands separated; tens above $1 000)."""
    step = Decimal(10) if abs(value) >= 1000 else Decimal(1)
    rounded = int((value / step).quantize(Decimal(1), ROUND_HALF_UP) * step)
    return "$" + f"{rounded:,}".replace(",", " ")


def plain(value: Decimal) -> str:
    text = f"{value.quantize(Decimal('0.01'), ROUND_HALF_UP):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def lots_word(lots: Decimal) -> str:
    if lots != lots.to_integral_value():
        return "лота"
    n = int(lots) % 100
    if 11 <= n <= 14:
        return "лотов"
    return {1: "лот", 2: "лота", 3: "лота", 4: "лота"}.get(n % 10, "лотов")


def size_lines(signal: IctSignal, settings: Settings) -> list[str]:
    lines = []
    if signal.units is not None:
        unit = "oz" if "XAU" in signal.ticker else signal.ticker.removesuffix("USDT")
        size = f"Размер: {plain(signal.units)} {escape(unit)}"
        contract = lookup(settings.tv_contract_sizes, signal.ticker)
        if contract is not None:
            lots = (signal.units / Decimal(contract)).quantize(
                Decimal("0.01"), ROUND_HALF_UP
            )
            size += f" = {plain(lots)} {lots_word(lots)}"
        if signal.risk_usd is not None:
            size += f" · риск {money(signal.risk_usd)}"
        lines.append(size)
    elif signal.risk_usd is not None:
        lines.append(f"Риск: {money(signal.risk_usd)}")
    if signal.units is not None and signal.entry is not None:
        notional = signal.units * signal.entry
        leverage = signal.leverage
        if leverage is not None and leverage > 1:
            lines.append(f"Сумма при x{plain(leverage)}: {money(notional / leverage)}")
        else:
            lines.append(f"Сумма позиции: {money(notional)}")
    return lines


def _when(at: datetime, now: datetime, settings: Settings) -> str:
    local, today = at.astimezone(settings.tzinfo), now.astimezone(settings.tzinfo)
    day = (local.date() - today.date()).days
    label = {0: "сегодня", 1: "завтра"}.get(day, f"{local:%d.%m}")
    return f"{label} {local:%H:%M}"


def background(context: EntryContext, now: datetime, settings: Settings) -> str:
    parts = []
    if not context.calendar_ok:
        parts.append("календарь недоступен")
    elif context.next_event is not None:
        event = context.next_event
        parts.append(
            f"{escape(event.title)} {_when(event.at, now, settings)} "
            f"({_until(event.at, now)})"
        )
    else:
        parts.append("важных событий в ближайшие 24 ч нет")
    macro = context.macro
    for name, label in (("DXY", "DXY"), ("US10Y", "10Y")):
        trend = (macro.get(name) or {}).get("trend")
        if trend in ARROW:
            parts.append(f"{label} {ARROW[trend]}")
    vix = (macro.get("VIX") or {}).get("vix")
    if isinstance(vix, int | float):
        parts.append(f"VIX {vix:.0f}")
    return "📰 Фон: " + " · ".join(parts)


def format_entry(
    signal: IctSignal, context: EntryContext, settings: Settings, now: datetime
) -> str:
    places = decimals(signal, settings)
    sell = signal.side == "SELL"
    icon = "🔴" if sell else "🟢"
    tf = timeframe_label(signal.tf)
    lines = []
    if signal.plan_id.upper().startswith("TEST-"):  # scripts/replay_tv.py
        lines += [
            "🧪 <b>TEST — NOT A REAL TRADING SIGNAL</b> · тест бота, не входить",
            "",
        ]
    lines += [
        (
            f"{icon} <b>{signal.side} — ВХОДИТЬ СЕЙЧАС по рынку</b> · "
            f"{escape(signal.ticker)} · {escape(tf)}"
        )
    ]
    model = escape(signal.model or "модель не указана")
    score = (
        f" · чек-лист {signal.score}/100 (это не вероятность)" if signal.score else ""
    )
    lines.append(f"Модель: {model}{score}")
    lines.append(
        "Подтверждение: цена была в зоне, свеча закрылась "
        f"{'вниз' if sell else 'вверх'} из зоны"
    )
    if (
        context.late_minutes is not None
        and context.late_minutes > settings.tv_late_warn_minutes
    ):
        lines.append(f"⚠️ сигнал запоздал на {context.late_minutes:.0f} мин")
    if context.news_now:
        event = context.news_now[0]
        lines.append(
            f"⚠️ <b>НОВОСТИ: осторожно</b> — {escape(event.title)} "
            f"{_when(event.at, now, settings)}"
        )

    entry, sl = signal.entry, signal.sl
    assert entry is not None and sl is not None  # checked by check_levels
    risk = abs(entry - sl)
    closed = candle_close(signal)
    city = CITY.get(settings.timezone, f"({escape(settings.timezone)})")
    when = (
        f"  (закрытие свечи {_when(closed, now, settings).removeprefix('сегодня ')} "
        f"{city})"
        if closed is not None
        else ""
    )

    def target(value: Decimal | None, name: str | None, first: bool) -> str:
        if value is None:
            return "—"
        r = r_label(float(abs(value - entry) / risk)) if risk else "?R"
        if first:
            return (
                f"{price(value, places)}   ({r} — закрыть половину, стоп в безубыток)"
            )
        label = f"{escape(name)}, {r}" if name else r
        return f"{price(value, places)}   ({label})"

    block = [
        f"Вход:  {price(entry, places)}{when}",
        f"Стоп:  {price(sl, places)}   (риск {price(risk, places)})",
        f"TP1:   {target(signal.tp1, None, True)}",
    ]
    if signal.tp2 is not None:
        block.append(f"TP2:   {target(signal.tp2, signal.tp2_name, False)}")
    if signal.tp3 is not None:
        block.append(f"TP3:   {target(signal.tp3, signal.tp3_name, False)}")
    lines += ["", "<pre>" + "\n".join(block) + "</pre>"]

    size = size_lines(signal, settings)
    if size:
        lines += ["", *size]
    lines.append("")
    if signal.checklist:
        lines.append(escape(signal.checklist))
    lines.append(f"Мой сканер: {escape(context.scanner)}")
    lines += ["", background(context, now, settings)]
    if signal.symbol:
        lines += [
            "",
            "📈 https://www.tradingview.com/chart/?symbol=" + escape(signal.symbol),
        ]
    return "\n".join(lines)


def news_window(
    events: list[EconomicEvent], at: datetime, settings: Settings
) -> list[EconomicEvent]:
    """High-impact events from 30 min before to 60 min after the signal."""
    before = timedelta(minutes=settings.tv_news_before_minutes)
    after = timedelta(minutes=settings.tv_news_after_minutes)
    return [e for e in events if e.at - before <= at <= e.at + after]
