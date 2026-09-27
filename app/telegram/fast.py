"""Russian Telegram messages for fast market events (EARLY WARNING, not a setup)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape

from app.fast.detector import FastState, Trigger
from app.telegram.i18n import catalog
from app.telegram.scanner import Mode

SEPARATOR = "━━━━━━━━━━━━━━━━━━"
WINDOW_RU = {
    "15s": "15 сек",
    "30s": "30 сек",
    "1m": "1 мин",
    "3m": "3 мин",
    "5m": "5 мин",
}


def tradingview_url(symbol: str, exchange: str = "BINGX") -> str:
    """Verified format: BINGX:ARBUSDT.P (TradingView symbol page, 2026-09-24)."""
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{symbol}.P"


@dataclass
class FastContext:
    """Everything known at detection time; None means 'нет данных', never guessed."""

    technical_score: int | None = None
    technical_state: str | None = None
    active_setup: str | None = None  # e.g. "LONG 73/100 · SETUP_FORMING"
    oi_change_pct: float | None = None
    oi_since: str | None = None  # HH:MM of the stored snapshot compared against
    news: str | None = None  # one-line HIGH/CRITICAL news reference
    alert_score: int = 80


def title(trigger: Trigger, decision: str, active: bool) -> str:
    if active and decision == "NEW":
        return "⚡ <b>ДВИЖЕНИЕ ПО АКТИВНОМУ СЕТАПУ</b>"
    if trigger.state == FastState.EXTREME_MOVE:
        return "🚨 <b>ЭКСТРЕМАЛЬНОЕ ДВИЖЕНИЕ</b>"
    if trigger.state == FastState.CONFIRMED_MOMENTUM:
        return "🔥 <b>ИМПУЛЬС ПОДТВЕРЖДАЕТСЯ</b>"
    if decision == "EXTENSION":
        return "⚡ <b>ДВИЖЕНИЕ ПРОДОЛЖАЕТСЯ</b>"
    return "⚡ <b>РЕЗКОЕ ДВИЖЕНИЕ</b>"


def why(trigger: Trigger) -> list[str]:
    lines = ["Цена движется значительно быстрее обычной волатильности этого актива."]
    if "VOLUME_EXPANSION" in trigger.reasons:
        lines.append("Движение сопровождается резким ростом объёма.")
    if trigger.state == FastState.EXTREME_MOVE:
        lines.append(
            "Масштаб движения исключителен относительно недавней нормы актива."
        )
    return lines


def format_fast_event(
    symbol: str,
    trigger: Trigger,
    decision: str,
    context: FastContext,
    detected_at: datetime,
    mode: Mode = "LIVE",
) -> str:
    na = "нет данных"
    icon = "🟢" if trigger.change_pct > 0 else "🔴"
    volume = (
        na
        if trigger.volume_ratio is None
        else f"повышенный (×{trigger.volume_ratio:.1f} к норме)"
        if "VOLUME_EXPANSION" in trigger.reasons
        else f"обычный (×{trigger.volume_ratio:.1f})"
    )
    oi = (
        na
        if context.oi_change_pct is None
        else f"{context.oi_change_pct:+.2f}% (с {context.oi_since})"
    )
    score = (
        na
        if context.technical_score is None
        else f"{context.technical_score} / 100 (последний закрытый анализ)"
    )
    confirmed = (
        context.technical_score is not None
        and context.technical_score >= context.alert_score
    )
    lines = []
    if mode == "TEST":
        lines.append("🧪 <b>TEST — NOT A REAL TRADING SIGNAL</b>")
    if mode != "LIVE":
        lines += [catalog("ru")[f"mode.{mode}"], ""]
    lines += [
        title(trigger, decision, context.active_setup is not None),
        SEPARATOR,
        f"🪙 <b>{escape(symbol)}</b> · BingX",
        f"{icon} <b>{trigger.change_pct:+.2f}%</b> за {WINDOW_RU[trigger.window]}",
        f"📏 Порог этого актива: {trigger.threshold_pct:.2f}% (адаптивный, по волатильности)",
        "",
        f"🔊 Объём: {volume}",
        f"📊 OI: {oi}",
        f"⭐ Technical score: {score}",
        f"🧱 Current state: {escape(context.technical_state or na)}",
    ]
    if context.active_setup:
        lines.append(
            f"🧩 Активный setup: {escape(context.active_setup)} — оценка не меняется"
        )
    if context.news:
        lines.append(f"📰 {escape(context.news)}")
    lines += [
        "",
        "💡 <b>Почему пришло уведомление:</b>",
        *why(trigger),
        "",
        "⚠️ Это раннее предупреждение.",
        "Технический setup ≥80 уже есть — см. основной сигнал."
        if confirmed
        else "Полный HIGH_CONFLUENCE setup ещё не подтверждён.",
        "",
        "🧭 <b>Что отслеживать:</b>",
        "• удержание импульса",
        "• продолжение объёма",
        "• OI",
        "• ближайшую S/R зону",
        "• BTC/ETH context",
        "",
        f"🔎 Причина: {', '.join(trigger.reasons)} · {decision}",
        f"🕐 Обнаружено: {detected_at.strftime('%d.%m.%Y · %H:%M:%S UTC')}",
        catalog("ru")[f"footer.{mode}"],
        SEPARATOR,
        "<i>Раннее предупреждение, не торговый сигнал. Автоматической торговли нет.</i>",
    ]
    return "\n".join(lines)
