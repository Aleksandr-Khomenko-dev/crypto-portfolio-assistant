"""Telegram scanner messages (HTML parse mode). Presentation only: every line is derived
deterministically from the stored setup/result; nothing here affects scoring or alerts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Literal

from app.scanner.domain import (
    Direction,
    Readiness,
    ScannerResult,
    Setup,
    SetupRead,
    StructureBreak,
    Zone,
)
from app.telegram.i18n import catalog

EXCHANGE_NAMES = {"BINGX": "BingX", "BINANCE": "Binance"}
Mode = Literal["LIVE", "TEST", "DRY_RUN"]
DIRECTION_ICON = {Direction.LONG: "🟢", Direction.SHORT: "🔴"}
DIRECTION_BADGE = {d: f"{icon} {d.value}" for d, icon in DIRECTION_ICON.items()}


def price(value: Decimal | None, na: str) -> str:
    """Plain decimal with ~6 significant digits; never scientific notation."""
    if value is None:
        return na
    value = Decimal(value)
    if value == 0:
        return "0"
    digits = value.adjusted()  # exponent of the leading digit
    places = max(0, 5 - digits) if digits >= 0 else 3 - digits
    return f"{value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP):f}"


def number(value: float | None, na: str, places: int = 0, suffix: str = "") -> str:
    return na if value is None else f"{value:.{places}f}{suffix}"


def percent(value: float | None, na: str) -> str:
    return na if value is None else f"{value:+.2f}%"


def closed_label(closed_at: datetime) -> str:
    # Stored close times end 1 ms before the boundary; show the boundary itself.
    boundary = closed_at + timedelta(milliseconds=1)
    return boundary.strftime("%d.%m.%Y · %H:%M UTC")


def dot(value: str, direction: Direction) -> str:
    wanted = "BULLISH" if direction == Direction.LONG else "BEARISH"
    against = "BEARISH" if direction == Direction.LONG else "BULLISH"
    return "🟢" if value == wanted else "🔴" if value == against else "⚪"


def zone_text(zones: list[Zone], na: str) -> str:
    if not zones:
        return na
    return f"<code>{escape(price(zones[0].lower, na))} – {escape(price(zones[0].upper, na))}</code>"


def _break_text(event: StructureBreak | None, trend: str, t: dict[str, str]) -> str:
    if event is None:
        return t.get(f"trend.{trend}", escape(trend))
    if event.kind == "INITIAL_BREAK":
        return f"{t['break.INITIAL_BREAK']} {t[f'break.{event.direction}']}"
    return f"{event.kind} {t[f'break.{event.direction}']}"


def _break_trend(event: StructureBreak | None, trend: str) -> str:
    if event is None:
        return trend
    return "BULLISH" if event.direction == Direction.LONG else "BEARISH"


def _market_line(result: ScannerResult, symbol: str) -> str:
    name = EXCHANGE_NAMES.get(result.exchange, result.exchange)
    return f"🪙 <b>{escape(symbol)}</b> · {escape(name)}"


def _footer(result: ScannerResult, mode: Mode, t: dict[str, str]) -> list[str]:
    return [
        t["closed_candle"].format(value=closed_label(result.candle_closed_at)),
        t[f"footer.{mode}"],
        t["separator"],
        t["disclaimer"],
    ]


def _meaning(setup: Setup, result: ScannerResult, t: dict[str, str]) -> list[str]:
    wanted = "BULLISH" if setup.direction == Direction.LONG else "BEARISH"
    trends = [
        result.frames["4h"].technical.alignment,
        result.frames["1h"].macro.trend,
    ]
    aligned = sum(value == wanted for value in trends)
    against = sum(value not in (wanted, "MIXED", "NEUTRAL") for value in trends)
    lines = []
    if aligned == 2:
        lines.append(t["meaning.trend_both"])
    elif aligned == 1:
        lines.append(t["meaning.trend_one"])
    elif against:
        lines.append(t["meaning.trend_against"])
    micro = result.frames[result.signal_timeframe].micro
    event = micro.breaks[-1] if micro.breaks else None
    blocks = setup.blocks
    if event and event.direction == setup.direction and event.kind in ("BOS", "CHoCH"):
        lines.append(t["meaning.break"].format(kind=event.kind))
    for block, key in (
        ("location", "meaning.location"),
        ("trigger", "meaning.trigger"),
        ("volume_momentum", "meaning.volume"),
        ("market_context", "meaning.context"),
    ):
        # A block scores only when its condition held; its points are the evidence.
        if blocks.get(block, 0) > (2 if block == "market_context" else 0):
            lines.append(t[key])
    return lines[:3] or [t["meaning.weak"]]


def _watch_next(setup: Setup, result: ScannerResult, t: dict[str, str]) -> list[str]:
    na = t["na"]
    readiness = setup.readiness
    if readiness == Readiness.WAIT_LOCATION:
        items = [t[f"next.WAIT_LOCATION.{setup.direction}"]]
    else:
        items = [t[f"next.{readiness}"]]
    if readiness in (Readiness.WAIT_STRUCTURE, Readiness.WAIT_VOLUME):
        items.append(t["next.closed_candle"])
    if setup.risk.invalidation is not None:
        items.append(
            t[f"next.hold.{setup.direction}"].format(
                level=f"<code>{escape(price(setup.risk.invalidation, na))}</code>"
            )
        )
    horizons = result.derivatives.oi_change_by_horizon
    if result.derivatives.oi_history_source == "SELF_RECORDED" and not any(
        value is not None for value in horizons.values()
    ):
        items.append(t["next.oi_building"])
    state = result.context.state
    if state in ("RISK_OFF", "HIGH_VOLATILITY") or state == (
        "BEARISH" if setup.direction == Direction.LONG else "BULLISH"
    ):
        items.append(t["next.context"].format(value=t[f"context.{state}"]))
    return [f"• {item}" for item in items[:4]]


def _active(
    setup: Setup, result: ScannerResult, t: dict[str, str], news: list | None = None
) -> list[str]:
    na = t["na"]
    frame = result.frames[result.signal_timeframe]
    tech = frame.technical
    derivative = result.derivatives
    icon, title = t[f"icon.{setup.state}"], t[f"state.{setup.state}"]
    ready = setup.readiness == Readiness.READY
    status = (
        f"✅ <b>{t['status'].format(status=t['ready'].upper())}</b>"
        if ready
        else "🟡 "
        + t["status"].format(
            status=t["status.watch"].format(reason=t[f"readiness.{setup.readiness}"])
        )
    )
    event = frame.micro.breaks[-1] if frame.micro.breaks else None
    trend_4h = result.frames["4h"].technical.alignment
    trend_1h = result.frames["1h"].macro.trend
    horizons = derivative.oi_change_by_horizon
    # Older snapshots have no horizon map; their 15m change is oi_change_pct.
    oi_15m = horizons.get("15m") if horizons else derivative.oi_change_pct
    funding = na
    if derivative.funding_rate is not None:
        per = (
            f"/{derivative.funding_interval_hours}ч"
            if derivative.funding_interval_hours
            else ""
        )
        funding = (
            f"{derivative.funding_rate * 100:.4f}%{per} · "
            f"{t.get(f'funding.{derivative.funding_state}', na)}"
        )
    risk_mark = t.get(f"risk.{setup.risk.reason}", "⚠️")
    rr = (
        f"<b>{setup.risk.rr:.2f}</b> {risk_mark}"
        if setup.risk.rr is not None
        else f"{na} {risk_mark}"
    )
    oi_meaning = t.get(f"oi.{derivative.interpretation}")
    context = result.context.state
    lines = [
        f"{icon} <b>{title.upper()}</b> · {DIRECTION_BADGE[setup.direction]}",
        t["separator"],
        _market_line(result, setup.symbol),
        f"🕐 Таймфрейм сигнала: {result.signal_timeframe.upper()} · контекст: 4H",
        t["score"].format(score=setup.score),
        t["score_note"],
        "",
        f"{DIRECTION_ICON[setup.direction]} "
        + t["direction"].format(direction=setup.direction.value),
        status,
        "",
        t["section.structure"],
        f"{dot(trend_4h, setup.direction)} "
        + t["trend_4h"].format(value=t.get(f"trend.{trend_4h}", escape(trend_4h))),
        f"{dot(trend_1h, setup.direction)} "
        + t["context_1h"].format(value=t.get(f"trend.{trend_1h}", escape(trend_1h))),
        f"{dot(_break_trend(event, frame.micro.trend), setup.direction)} "
        + t[f"structure_{result.signal_timeframe}"].format(value=_break_text(event, frame.micro.trend, t)),
        f"{dot(tech.alignment, setup.direction)} "
        + t[f"ema_{result.signal_timeframe}"].format(
            value=t.get(f"ema.{tech.alignment}", escape(tech.alignment))
        ),
        t["indicators"].format(
            rsi=number(tech.rsi, na),
            adx=number(tech.adx, na),
            rvol=number(tech.rvol, na, 2, "x"),
        ),
        "",
        t["section.derivatives"],
        t["funding"].format(value=funding),
        t["oi"].format(horizon="15m", value=percent(oi_15m, na)),
        t["oi"].format(horizon="1H", value=percent(horizons.get("1h"), na)),
        t["oi"].format(horizon="4H", value=percent(horizons.get("4h"), na)),
        *([f"<i>{oi_meaning}</i>"] if oi_meaning else []),
        "",
        t["section.context"],
        f"{dot(context, setup.direction)} "
        + t["market"].format(value=t.get(f"context.{context}", escape(context))),
        "",
        t["section.levels"],
        t["support"].format(value=zone_text(frame.supports, na)),
        t["resistance"].format(value=zone_text(frame.resistances, na)),
        t["invalidation"].format(
            value=f"<code>{escape(price(setup.risk.invalidation, na))}</code>"
            if setup.risk.invalidation is not None
            else na
        ),
        t["rr"].format(value=rr),
        "",
        t["section.meaning"],
        *_meaning(setup, result, t),
        "",
        t["section.next"],
        *_watch_next(setup, result, t),
        "",
    ]
    if news:
        from app.telegram.news import format_news_block

        lines += [*format_news_block(news, result.created_at), ""]
    lines.append(t["details"].format(symbol=escape(setup.symbol)))
    return lines


def _terminal(
    setup: Setup,
    result: ScannerResult,
    lifecycle: str,
    t: dict[str, str],
    episode_invalidation: Decimal | None,
) -> list[str]:
    na = t["na"]
    long = setup.direction == Direction.LONG
    arrow = "📉" if long else "📈"
    direction = setup.direction.value
    if lifecycle == "INVALIDATED":
        level = (
            episode_invalidation
            if episode_invalidation is not None
            else setup.risk.invalidation
        )
        return [
            f"❌ <b>{t['invalidated'].upper()}</b>",
            t["separator"],
            _market_line(result, setup.symbol),
            "",
            t["terminal.no_longer_active"].format(arrow=arrow, direction=direction),
            t["last_score"].format(score=setup.score),
            "",
            t["terminal.broken_level"].format(
                level=f"<code>{escape(price(level, na))}</code>"
                if level is not None
                else na
            ),
            "",
            t["section.meaning"],
            t["terminal.invalidated_meaning"],
            "",
            t["terminal.dont_use"],
            t["terminal.wait_new"],
        ]
    return [
        f"⌛ <b>{t['expired'].upper()}</b>",
        t["separator"],
        _market_line(result, setup.symbol),
        "",
        t["terminal.expired_body"].format(arrow="⏳", direction=direction),
        t["last_score"].format(score=setup.score),
        "",
        t["section.meaning"],
        t["terminal.expired_meaning"],
        "",
        t["terminal.wait_new"],
    ]


def format_setup(
    setup: Setup,
    result: ScannerResult,
    lifecycle: str = "ACTIVE",
    *,
    mode: Mode = "LIVE",
    language: str = "ru",
    episode_invalidation: Decimal | None = None,
    news: list | None = None,
) -> str:
    """`news`: at most 3 NewsContext items known at signal time (context only)."""
    t = catalog(language)
    body = (
        _terminal(setup, result, lifecycle, t, episode_invalidation)
        if lifecycle in ("INVALIDATED", "EXPIRED")
        else _active(setup, result, t, news)
    )
    prefix = [t[f"mode.{mode}"], ""] if mode != "LIVE" else []
    return "\n".join([*prefix, *body, "", *_footer(result, mode, t)])


def format_ranking(
    setups: list[SetupRead],
    direction: Direction | None,
    language: str = "ru",
) -> str:
    t = catalog(language)
    title = t[f"ranking.title.{direction.value if direction else 'ALL'}"]
    lines = [f"<b>{escape(title)}</b>", t["ranking.note"], ""]
    lines += [
        f"{i}. {DIRECTION_ICON[s.direction]} <b>{escape(s.symbol)}</b> · "
        f"{s.score}/100 · "
        + (
            "✅ " + t["ready"].lower()
            if s.readiness == Readiness.READY
            else t[f"readiness.{s.readiness}"]
        )
        for i, s in enumerate(
            sorted(setups, key=lambda s: (-s.score, s.symbol))[:10], 1
        )
    ]
    if not setups:
        lines.append(t["ranking.empty"])
    return "\n".join(lines)
