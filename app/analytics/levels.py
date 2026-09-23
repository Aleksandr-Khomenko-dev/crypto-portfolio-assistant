from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.analytics.technical import true_ranges, wilder
from app.scanner.domain import FVG, Candle, Direction, FVGStatus, Pivot, Timeframe, Zone


def zones(
    bars: list[Candle], points: list[Pivot], atr: float, width: float
) -> tuple[list[Zone], list[Zone]]:
    # Anchor each zone at its first confirmed pivot, using ATR available then.
    # Later pivots may join it, but cannot retroactively move its center or width.
    historical_atr = wilder(true_ranges(bars), 14) if len(bars) >= 14 else []
    groups: list[tuple[Pivot, Decimal, Decimal]] = []
    for pivot in sorted(points, key=lambda p: p.confirmed_index):
        if historical_atr and pivot.confirmed_index < 13:
            continue
        value = historical_atr[pivot.confirmed_index - 13] if historical_atr else atr
        tolerance = Decimal(str(value * width))
        if not any(lower <= pivot.price <= upper for _, lower, upper in groups):
            groups.append((pivot, pivot.price - tolerance, pivot.price + tolerance))
    support: list[Zone] = []
    resistance: list[Zone] = []
    price = bars[-1].close
    for pivot, lower, upper in groups:
        first = pivot.confirmed_index
        # The pivot bar itself is the first touch; all counted bars are closed history.
        interactions, was_inside = 0, False
        for bar in bars[max(pivot.index, 0) :]:
            inside = bar.low <= upper and bar.high >= lower
            interactions += int(inside and not was_inside)
            was_inside = inside
        distance = max(lower - price, price - upper, Decimal(0)) / Decimal(str(atr))
        zone = Zone(
            lower=lower,
            upper=upper,
            created_at=bars[first].close_time,
            age_bars=len(bars) - 1 - first,
            interactions=interactions,
            distance_atr=float(distance),
        )
        (support if (lower + upper) / 2 <= price else resistance).append(zone)
    support.sort(key=lambda z: z.upper, reverse=True)
    resistance.sort(key=lambda z: z.lower)
    return support, resistance


def fair_value_gaps(
    bars: list[Candle],
    timeframe: Timeframe,
    min_atr: float,
    previous: list[FVG] | None = None,
    previous_closed_at: datetime | None = None,
) -> list[FVG]:
    """Filter by ATR known at formation, then track subsequent mitigation and invalidation."""
    if len(bars) < 14:
        return []
    atr_values = wilder(true_ranges(bars), 14)
    new_bars = [
        b
        for b in bars
        if previous_closed_at is None or b.close_time > previous_closed_at
    ]
    # Keep only gaps formed inside the supplied window, exactly as a cold rebuild would,
    # so unmitigated historical gaps cannot accumulate without bound.
    result = [
        mitigate_gap(gap, new_bars)
        for gap in (previous or [])
        if gap.created_at >= bars[13].close_time
    ]
    for i in range(13, len(bars)):
        current, older = bars[i], bars[i - 2]
        if previous_closed_at is not None and current.close_time <= previous_closed_at:
            continue
        if current.low > older.high:
            lower, upper, direction = older.high, current.low, Direction.LONG
        elif current.high < older.low:
            lower, upper, direction = current.high, older.low, Direction.SHORT
        else:
            continue
        if float(upper - lower) < atr_values[i - 13] * min_atr:
            continue
        gap = FVG(
            lower=lower,
            upper=upper,
            midpoint=(lower + upper) / 2,
            created_at=current.close_time,
            timeframe=timeframe,
            direction=direction,
        )
        gap = mitigate_gap(gap, bars[i + 1 :])
        result.append(gap)
    return result


def mitigate_gap(gap: FVG, bars: list[Candle]) -> FVG:
    gap = gap.model_copy(deep=True)
    if gap.status in ("FILLED", "INVALIDATED"):
        return gap
    lower, upper, direction = gap.lower, gap.upper, gap.direction
    rank = {"FRESH": 0, "PARTIALLY_MITIGATED": 1, "CE_TOUCHED": 2, "FILLED": 3}
    for bar in bars:
        if (direction == Direction.LONG and bar.close < lower) or (
            direction == Direction.SHORT and bar.close > upper
        ):
            gap.status = "INVALIDATED"
            break
        depth = bar.low if direction == Direction.LONG else bar.high
        filled = depth <= lower if direction == Direction.LONG else depth >= upper
        ce = (
            depth <= gap.midpoint
            if direction == Direction.LONG
            else depth >= gap.midpoint
        )
        partial = depth <= upper if direction == Direction.LONG else depth >= lower
        status: FVGStatus = (
            "FILLED"
            if filled
            else "CE_TOUCHED"
            if ce
            else "PARTIALLY_MITIGATED"
            if partial
            else "FRESH"
        )
        if rank[status] > rank[gap.status]:
            gap.status = status
        if gap.status == "FILLED":
            break  # Filled gaps are terminal; later prices cannot rewrite their outcome.
    return gap
