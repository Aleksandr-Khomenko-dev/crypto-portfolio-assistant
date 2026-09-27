"""Russian Telegram messages for early events (EARLY WARNING, never a trade signal).

Every message answers: what happened, why it was detected, where the zone is, how
far price already moved, what is NOT confirmed yet, and what to watch next. Dynamic
values are HTML-escaped; unknown values are shown as "нет данных", never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html import escape

from app.early.model import EarlyEvent, EventType, KeyZone
from app.telegram.i18n import catalog
from app.telegram.scanner import Mode, price

SEPARATOR = "━━━━━━━━━━━━━━━━━━"
NA = "нет данных"

TITLE = {
    EventType.ZONE_WATCH: "👀 <b>ЗОНА ПОД НАБЛЮДЕНИЕМ</b>",
    EventType.BULLISH_IGNITION: "🌱 <b>РАННИЙ LONG-КОНТЕКСТ</b>",
    EventType.BEARISH_IGNITION: "🌱 <b>РАННИЙ SHORT-КОНТЕКСТ</b>",
    EventType.FORMATION_WATCH: "👀 <b>ВОЗМОЖНОЕ ФОРМИРОВАНИЕ</b>",
    EventType.BREAKOUT_APPROACH: "🟡 <b>ПОДХОД К ПРОБОЮ</b>",
    EventType.FIRST_BREAK: "⚡ <b>ПЕРВОЕ ПРОБИТИЕ</b>",
    EventType.MOMENTUM_CONFIRMED: "🔥 <b>ИМПУЛЬС ПОДТВЕРЖДАЕТСЯ</b>",
    EventType.RETEST_WATCH: "🔄 <b>РЕТЕСТ ЗОНЫ</b>",
    EventType.RETEST_CONFIRMED: "✅ <b>РЕТЕСТ ПОДТВЕРЖДЁН</b>",
    EventType.FAILED_BREAKOUT: "❌ <b>ЛОЖНЫЙ ПРОБОЙ</b>",
    EventType.LATE_EXTENDED_MOVE: "⚠️ <b>ДВИЖЕНИЕ УЖЕ РАСТЯНУТО</b>",
}
PATTERN_RU = {
    "ASCENDING_TRIANGLE": "Ascending Triangle",
    "DESCENDING_TRIANGLE": "Descending Triangle",
    "BULL_FLAG": "Bull Flag (continuation)",
    "BEAR_FLAG": "Bear Flag (continuation)",
    "COMPRESSION_AT_LEVEL": "Сжатие у уровня",
    "BREAKOUT_COMPRESSION": "Сжатие перед пробоем",
    "SWEEP_RECLAIM": "Снятие ликвидности + reclaim",
}
KIND_RU = {"SUPPORT": "support", "RESISTANCE": "resistance"}


@dataclass
class EarlyContext:
    """Read-only context at dispatch; None means нет данных (never guessed)."""

    technical_score: int | None = None
    technical_state: str | None = None
    active_setup: str | None = None
    oi_change_pct: float | None = None  # live OI vs the last stored 15m boundary
    oi_since: str | None = None
    funding_rate: float | None = None
    funding_state: str | None = None
    market: dict[str, str] = field(default_factory=dict)
    news: str | None = None
    alert_score: int = 80


def _p(value: float | None) -> str:
    return price(Decimal(str(value)), NA) if value is not None else NA


def _pct(value: float | None) -> str:
    return NA if value is None else f"{value:+.2f}%"


def _tf(zone: KeyZone | None) -> str:
    if zone is None:
        return ""
    return zone.timeframe if zone.timeframe in ("5m", "15m") else zone.timeframe.upper()


def zone_label(zone: KeyZone | None) -> str:
    if zone is None:
        return NA
    tf = _tf(zone)
    if zone.source == "PATTERN":
        return f"граница фигуры ({zone.timeframe})"
    if zone.source == "BREAK_LEVEL":
        return f"{tf} уровень прошлого пробоя"
    return f"{tf} {KIND_RU.get(zone.kind, zone.kind.lower())}"


def zone_range(zone: KeyZone | None) -> str:
    if zone is None:
        return NA
    if zone.lower == zone.upper:
        return _p(zone.lower)
    return f"{_p(zone.lower)}–{_p(zone.upper)}"


def _what(event: EarlyEvent) -> list[str]:
    t, z = event.event_type, event.zone
    long = event.direction == "LONG"
    if t == EventType.ZONE_WATCH:
        kind = "поддержку" if z and z.kind == "SUPPORT" else "сопротивление"
        return [f"Цена вошла в важную {kind}.", "Пока разворот не подтверждён."]
    if t in (EventType.BULLISH_IGNITION, EventType.BEARISH_IGNITION):
        tf = _tf(z)
        side = "поддержки" if long else "сопротивления"
        return [f"Цена начала реакцию от {tf} {side}."]
    if t == EventType.FORMATION_WATCH and event.pattern is not None:
        p = event.pattern
        lines = [f"Кандидат: {escape(PATTERN_RU.get(p.pattern_type, p.pattern_type))}."]
        if p.pattern_type == "ASCENDING_TRIANGLE":
            lines.append("Higher lows сохраняются. Цена близко к сопротивлению.")
        elif p.pattern_type == "DESCENDING_TRIANGLE":
            lines.append("Lower highs сохраняются. Цена близко к поддержке.")
        return [*lines, "Фигура ещё НЕ подтверждена."]
    if t == EventType.BREAKOUT_APPROACH:
        return ["Цена приближается к уровню.", "Пробоя пока нет."]
    if t == EventType.FIRST_BREAK:
        what = "сопротивление" if long else "поддержка"
        return [
            f"Пробито {what}: {zone_range(z)}",
            "Важно: пробой пока intrabar.",
            "Закрепление ещё не подтверждено.",
        ]
    if t == EventType.RETEST_WATCH:
        was = "пробитому сопротивлению" if long else "пробитой поддержке"
        becomes = "поддержкой" if long else "сопротивлением"
        return [
            f"Цена вернулась к ранее {was}: {zone_range(z)}",
            f"Сейчас проверяется, станет ли бывший уровень {becomes}.",
        ]
    if t == EventType.RETEST_CONFIRMED:
        return ["Пробитая зона удержалась."]
    if t == EventType.FAILED_BREAKOUT:
        return ["Уровень не удержан.", "Цена вернулась внутрь предыдущего диапазона."]
    if t == EventType.MOMENTUM_CONFIRMED:
        return ["Движение после пробоя получает подтверждения."]
    if t == EventType.LATE_EXTENDED_MOVE:
        return [
            "Импульс начался раньше.",
            "Текущая цена уже находится далеко от исходной зоны.",
            f"Это НЕ новый ранний {event.direction}-alert.",
        ]
    return []


def _not_confirmed(event: EarlyEvent, context: EarlyContext) -> str:
    score = context.technical_score
    if score is not None and score >= context.alert_score:
        return f"Основной технический setup ≥{context.alert_score} уже есть — см. основной сигнал."
    if event.event_type == EventType.FAILED_BREAKOUT:
        return "Сценарий пробоя отменён; это не сигнал в обратную сторону."
    return "Основной HIGH_CONFLUENCE ещё НЕ сформирован."


def _watch_next(event: EarlyEvent) -> list[str]:
    t, z = event.event_type, zone_range(event.zone)
    long = event.direction == "LONG"
    if t == EventType.ZONE_WATCH:
        return ["реакцией цены", "снятием ликвидности", "5m CHoCH/BOS", "объёмом", "OI"]
    if t in (EventType.BULLISH_IGNITION, EventType.BEARISH_IGNITION):
        hold = "удержание поддержки" if long else "удержание сопротивления"
        return [hold, "первый 5m BOS", "продолжение объёма", "OI", "BTC/ETH context"]
    if t == EventType.FORMATION_WATCH:
        return ["пробой границы фигуры", "объём на пробое", "инвалидацию фигуры"]
    if t == EventType.BREAKOUT_APPROACH:
        return ["реакцию на уровне", "объём на подходе", "первое пробитие / отбой"]
    if t in (EventType.FIRST_BREAK, EventType.MOMENTUM_CONFIRMED):
        return ["удержание уровня", "продолжение объёма", f"ретест {z}"]
    if t == EventType.RETEST_WATCH:
        return ["отбой от зоны", "micro BOS", "объём", "OI", "BTC/ETH context"]
    if t == EventType.RETEST_CONFIRMED:
        return [
            "удержание ретеста",
            "15m закрытие за уровнем",
            "основной score (закрытые свечи)",
        ]
    if t == EventType.FAILED_BREAKOUT:
        return ["поведение внутри диапазона", "новую попытку пробоя"]
    return [
        "откат к исходной зоне",
        "ослабление импульса",
        "основной score (закрытые свечи)",
    ]


def format_early_event(
    event: EarlyEvent,
    context: EarlyContext,
    detected_at: datetime,
    mode: Mode = "LIVE",
) -> str:
    m = event.metrics
    icon = "🟢" if event.direction == "LONG" else "🔴"
    lines: list[str] = []
    if mode == "TEST":
        lines.append("🧪 <b>TEST — NOT A REAL TRADING SIGNAL</b>")
    if mode != "LIVE":
        lines += [catalog("ru")[f"mode.{mode}"], ""]
    title = TITLE.get(event.event_type, escape(event.event_type.value))
    if event.decision == "UPGRADE":
        title += " · усиление"
    lines += [
        title,
        SEPARATOR,
        f"🪙 <b>{escape(event.symbol)}</b> · BingX",
        f"{icon} {event.direction} context",
    ]
    if event.pattern is not None:
        p = event.pattern
        lines.append(
            f"📐 {escape(PATTERN_RU.get(p.pattern_type, p.pattern_type))} · {escape(p.source_timeframe)}"
        )
        lines.append(f"🎯 Граница: {_p(p.boundary_level)}")
    elif event.zone is not None:
        lines.append(f"⏱ Зона: {escape(zone_label(event.zone))}")
        lines.append(f"🎯 {zone_range(event.zone)}")
    lines.append(f"💵 Текущая цена: {_p(event.price)}")
    lines += [""] + _what(event)
    if event.confirmations:
        header = (
            "Подтверждения:"
            if event.event_type
            in (EventType.RETEST_CONFIRMED, EventType.MOMENTUM_CONFIRMED)
            else "Что изменилось:"
        )
        lines += [
            "",
            header,
            *(f"• {escape(c[:120])}" for c in event.confirmations[:8]),
        ]
    lines.append("")
    distance = m.get("distance_to_level_atr", m.get("distance_to_zone_atr"))
    if distance is not None:
        lines.append(f"📏 Расстояние до уровня: {distance:.2f} ATR")
    if m.get("penetration_atr") is not None:
        lines.append(
            f"📏 Пробитие: {m['penetration_pct']:+.2f}% ({m['penetration_atr']:.2f} ATR), "
            f"держится {m.get('hold_seconds', 0):.0f} сек"
        )
    if m.get("distance_from_origin_atr") is not None:
        lines.append(
            f"📐 От начала движения: {m['distance_from_origin_atr']:.2f} ATR "
            f"({_pct(m.get('move_since_origin_pct'))})"
        )
    if (
        m.get("move_since_break_pct") is not None
        and event.event_type != EventType.FIRST_BREAK
    ):
        lines.append(f"📐 С момента пробоя: {_pct(m['move_since_break_pct'])}")
    moves = [
        f"1m {_pct(m['velocity_1m_pct'])}"
        if m.get("velocity_1m_pct") is not None
        else None,
        f"5m {_pct(m['move_5m_pct'])}" if m.get("move_5m_pct") is not None else None,
    ]
    if any(moves):
        lines.append("📈 Движение: " + " · ".join(x for x in moves if x))
    volume = m.get("volume_ratio")
    lines.append(f"🔊 RVOL: {NA if volume is None else f'{volume:.1f}x'}")
    if context.oi_change_pct is not None:
        lines.append(
            f"📊 OI: {context.oi_change_pct:+.2f}% (с {escape(context.oi_since or '?')})"
        )
    elif m.get("oi_change_15m_pct") is not None:
        lines.append(f"📊 OI 15m: {m['oi_change_15m_pct']:+.2f}%")
    else:
        lines.append(f"📊 OI: {NA}")
    if context.funding_rate is not None:
        state = (context.funding_state or "").lower() or NA
        lines.append(
            f"💸 Funding: {context.funding_rate * 100:+.4f}% ({escape(state)})"
        )
    else:
        lines.append(f"💸 Funding: {NA}")
    if context.market:
        market = " · ".join(
            f"{escape(k)}: {escape(v)}" for k, v in context.market.items()
        )
        lines.append(f"🌐 Рынок: {market}")
    score = context.technical_score
    lines.append(
        f"⭐ Основной technical score: {NA if score is None else f'{score} / 100'}"
        + ("" if score is None else " (последний закрытый анализ)")
    )
    if context.technical_state:
        lines.append(f"🧱 Состояние сканера: {escape(context.technical_state)}")
    if event.strength is not None:
        label = {
            EventType.FORMATION_WATCH: "Formation strength",
            EventType.BULLISH_IGNITION: "Ignition strength",
            EventType.BEARISH_IGNITION: "Ignition strength",
            EventType.BREAKOUT_APPROACH: "Approach strength",
            EventType.MOMENTUM_CONFIRMED: "Momentum strength",
            EventType.RETEST_CONFIRMED: "Confirmation strength",
        }.get(event.event_type, "Strength")
        lines.append(
            f"🧪 {label}: {event.strength} / 100 (диагностика, не вероятность)"
        )
    if context.active_setup:
        lines.append(
            f"🧩 Активный setup: {escape(context.active_setup)} — оценка не меняется"
        )
    if context.news:
        lines.append(f"📰 {escape(context.news[:200])}")
    lines += [
        "",
        "⚠️ Это ранний структурный alert.",
        _not_confirmed(event, context),
        "",
        "🧭 <b>Что смотреть дальше:</b>",
        *(f"• {escape(item)}" for item in _watch_next(event)),
        "",
        f"🔎 Событие: {event.event_type.value} · {escape(event.decision)}",
        f"🕐 Обнаружено: {detected_at.strftime('%d.%m.%Y · %H:%M:%S UTC')}",
        catalog("ru")[f"footer.{mode}"],
        SEPARATOR,
        (
            "<i>Раннее предупреждение, не торговый сигнал. Решение принимает "
            "пользователь. Автоматической торговли нет.</i>"
        ),
    ]
    # Every dynamic field is bounded (8 short confirmations, news cut to 200 chars),
    # so the message stays far below Telegram's 4096 limit without cutting HTML.
    return "\n".join(lines)
