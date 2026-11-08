"""Russian Telegram messages for fast market events (EARLY WARNING, not a setup)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape

from app.fast.detector import FastState, Trigger
from app.scanner.universe import display_symbol
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


# TradFi markets are read on the free OANDA reference chart, not on the exchange
# perpetual (every symbol checked in TradingView symbol search, 2026-11-09).
TRADINGVIEW_OVERRIDES = {
    "NCCOGOLD2USDUSDT": "OANDA:XAUUSD",
    "XAUUSDT": "OANDA:XAUUSD",
    "NCCOXAG2USDUSDT": "OANDA:XAGUSD",
    "NCCOXPT2USDUSDT": "OANDA:XPTUSD",
    "NCCOPALLADIUM2USDUSDT": "OANDA:XPDUSD",
    "NCCO724COPPER2USDUSDT": "OANDA:XCUUSD",
    "NCCO1OILWTI2USDUSDT": "OANDA:WTICOUSD",
    "NCCO1OILBRENT2USDUSDT": "OANDA:BCOUSD",
    "NCCO7241NATGAS2USDUSDT": "OANDA:NATGASUSD",
    "NCSINASDAQ1002USDUSDT": "OANDA:NAS100USD",
    "NCSISP5002USDUSDT": "OANDA:SPX500USD",
}
# BingX currency pairs: NCFX + BASE + 2 + QUOTE + USDT, e.g. NCFXEUR2USDUSDT.
FX_PAIR = re.compile(r"NCFX([A-Z]{3})2([A-Z]{3})USDT")


def tradingview_url(symbol: str, exchange: str = "BINGX") -> str:
    """Verified format: BINGX:ARBUSDT.P (TradingView symbol page, 2026-09-24)."""
    chart = "https://www.tradingview.com/chart/?symbol="
    if symbol in TRADINGVIEW_OVERRIDES:
        return chart + TRADINGVIEW_OVERRIDES[symbol]
    pair = FX_PAIR.fullmatch(symbol)
    if pair:
        return chart + f"OANDA:{pair.group(1)}{pair.group(2)}"
    return chart + f"{exchange}:{symbol}.P"


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
        f"🪙 <b>{escape(display_symbol(symbol))}</b> · BingX",
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
