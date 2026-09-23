from __future__ import annotations

from itertools import pairwise
from statistics import mean

from app.scanner.domain import Candle, Technical


def ema(values: list[float], period: int) -> list[float]:
    """SMA-seeded EMA; leading warmup values are omitted."""
    if period < 1 or len(values) < period:
        raise ValueError("Insufficient EMA history")
    result = [mean(values[:period])]
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def wilder(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        raise ValueError("Insufficient Wilder history")
    result = [mean(values[:period])]
    for value in values[period:]:
        result.append((result[-1] * (period - 1) + value) / period)
    return result


def true_ranges(bars: list[Candle]) -> list[float]:
    return [float(bars[0].high - bars[0].low)] + [
        float(
            max(
                bar.high - bar.low,
                abs(bar.high - prev.close),
                abs(bar.low - prev.close),
            )
        )
        for prev, bar in pairwise(bars)
    ]


def atr(bars: list[Candle], period: int = 14) -> float:
    return wilder(true_ranges(bars), period)[-1]


def rvol(bars: list[Candle], baseline: int = 20) -> float | None:
    if len(bars) < baseline + 1:
        raise ValueError("Insufficient RVOL history")
    average = mean(float(bar.volume) for bar in bars[-baseline - 1 : -1])
    return float(bars[-1].volume) / average if average > 0 else None


def rsi(values: list[float], period: int = 14) -> float:
    changes = [b - a for a, b in pairwise(values)]
    gain = wilder([max(c, 0) for c in changes], period)[-1]
    loss = wilder([max(-c, 0) for c in changes], period)[-1]
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100 - 100 / (1 + gain / loss)


def adx(bars: list[Candle], period: int = 14) -> tuple[float, float, float]:
    plus, minus = [], []
    for prev, bar in pairwise(bars):
        up, down = float(bar.high - prev.high), float(prev.low - bar.low)
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    ranges = wilder(true_ranges(bars)[1:], period)
    positive = [
        100 * p / tr if tr else 0.0 for p, tr in zip(wilder(plus, period), ranges)
    ]
    negative = [
        100 * m / tr if tr else 0.0 for m, tr in zip(wilder(minus, period), ranges)
    ]
    dx = [
        100 * abs(p - m) / (p + m) if p + m else 0.0 for p, m in zip(positive, negative)
    ]
    return wilder(dx, period)[-1], positive[-1], negative[-1]


def technical(bars: list[Candle], baseline: int = 20) -> Technical:
    if len(bars) < 210:
        raise ValueError("At least 210 closed bars required for EMA200 warmup")
    values = [float(b.close) for b in bars]
    volatility = atr(bars)
    if volatility <= 0:
        raise ValueError("Zero-volatility market")
    series = {str(p): ema(values, p) for p in (8, 21, 50, 200)}
    averages = {p: s[-1] for p, s in series.items()}
    ordered = list(averages.values())
    alignment = (
        "BULLISH"
        if all(a > b for a, b in pairwise(ordered))
        else ("BEARISH" if all(a < b for a, b in pairwise(ordered)) else "MIXED")
    )
    spread = abs(series["8"][-1] - series["50"][-1])
    previous = abs(series["8"][-4] - series["50"][-4])
    strength, positive, negative = adx(bars)
    return Technical(
        ema=averages,
        price_above_ema={p: values[-1] > v for p, v in averages.items()},
        ema_slope_atr={
            p: (s[-1] - s[-4]) / (3 * volatility) for p, s in series.items()
        },
        alignment=alignment,
        expansion="EXPANDING" if spread > previous else "COMPRESSING",
        atr=volatility,
        atr_pct=volatility / values[-1] * 100,
        rvol=rvol(bars, baseline),
        rsi=rsi(values),
        adx=strength,
        plus_di=positive,
        minus_di=negative,
        extension_atr=abs(values[-1] - averages["21"]) / volatility,
        momentum_pct=(values[-1] / values[-5] - 1) * 100,
    )
