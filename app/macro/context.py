"""Gold macro backdrop (pure). Same rules as the QS PRO Gold TradingView script.

Gold is priced in dollars and pays no yield, so a rising dollar together with rising
real-rate proxies is the classic headwind:

* DXY trend: last closed 4h close vs its EMA50, and the EMA50 slope.
* US 10Y trend: latest daily par yield vs its 20-day EMA (daily data only).
* LONG headwind  = DXY bullish AND 10Y rising  -> state BEARISH (for gold).
* SHORT headwind = DXY bearish AND 10Y falling -> state BULLISH (for gold).
* Anything else is MIXED. Missing inputs are reported, never guessed.
* VIX risk-off = VIX at/above the panic level or a spike vs its 20-day average.
  Shorting gold into a fear spike is blocked (gold can still fall in a pure
  liquidity crunch, e.g. March 2020, so this is a filter, not a forecast).
"""

from __future__ import annotations

from datetime import date
from statistics import mean

from app.analytics.technical import ema
from app.config import Settings
from app.scanner.domain import Candle, MarketContext


def dxy_trend(bars: list[Candle], length: int) -> tuple[str, dict[str, str | float]]:
    closes = [float(bar.close) for bar in bars]
    if len(closes) < length + 2:
        return "unavailable", {}
    line = ema(closes, length)
    last, now, before = closes[-1], line[-1], line[-2]
    trend = "UP" if last > now > before else "DOWN" if last < now < before else "FLAT"
    return trend, {"close": round(last, 3), "ema": round(now, 3), "trend": trend}


def yield_trend(
    rows: list[tuple[date, float]], length: int
) -> tuple[str, dict[str, str | float]]:
    values = [value for _, value in rows]
    if len(values) < length + 1:
        return "unavailable", {}
    line = ema(values, length)
    last = values[-1]
    trend = "UP" if last > line[-1] else "DOWN" if last < line[-1] else "FLAT"
    return trend, {
        "yield_pct": last,
        "ema": round(line[-1], 3),
        "date": rows[-1][0].isoformat(),
        "trend": trend,
    }


def vix_state(
    current: float | None, history: list[tuple[date, float]], settings: Settings
) -> tuple[bool, dict[str, str | float]]:
    if current is None:
        return False, {}
    closes = [value for _, value in history[-20:]]
    average = mean(closes) if len(closes) >= 10 else None
    spike = average is not None and current >= average * settings.macro_vix_spike
    risk_off = current >= settings.macro_vix_panic or spike
    row: dict[str, str | float] = {
        "vix": current,
        "risk_off": "YES" if risk_off else "NO",
    }
    if average is not None:
        row["avg20"] = round(average, 2)
    return risk_off, row


def gold_macro_context(
    dxy_bars: list[Candle] | None,
    us10y: list[tuple[date, float]] | None,
    vix_now: float | None,
    vix_history: list[tuple[date, float]] | None,
    settings: Settings,
) -> MarketContext:
    dxy, dxy_row = dxy_trend(dxy_bars or [], settings.macro_dxy_ema)
    rates, rates_row = yield_trend(us10y or [], settings.macro_yield_ema_days)
    risk_off, vix_row = vix_state(vix_now, vix_history or [], settings)
    assets = {
        name: row
        for name, row in (("DXY", dxy_row), ("US10Y", rates_row), ("VIX", vix_row))
        if row
    }
    if dxy == "unavailable" and rates == "unavailable":
        return MarketContext(
            kind="GOLD_MACRO",
            assets=assets,
            risk_off=risk_off,
            explanation="DXY and US10Y data unavailable",
        )
    if dxy == "UP" and rates == "UP":
        state, why = "BEARISH", "DXY and US10Y rising: headwind for gold LONG"
    elif dxy == "DOWN" and rates == "DOWN":
        state, why = "BULLISH", "DXY and US10Y falling: headwind for gold SHORT"
    else:
        state, why = "MIXED", f"DXY {dxy}, US10Y {rates}: no joint headwind"
    if risk_off:
        why += "; VIX risk-off: no gold SHORT"
    return MarketContext(
        kind="GOLD_MACRO",
        state=state,
        assets=assets,
        risk_off=risk_off,
        explanation=why,
    )
