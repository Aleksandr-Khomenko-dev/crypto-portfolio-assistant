from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import cast

from app.scanner.domain import (
    BreakKind,
    Candle,
    Direction,
    Pivot,
    PivotKind,
    Structure,
    StructureBreak,
)


def pivots(bars: list[Candle], length: int) -> list[Pivot]:
    """A pivot becomes known only after `length` right-hand CLOSED bars."""
    if length < 1:
        raise ValueError("Pivot length must be positive")
    result: list[Pivot] = []
    previous: dict[str, Decimal] = {}
    for i in range(length, len(bars) - length):
        neighbours = bars[i - length : i] + bars[i + 1 : i + length + 1]
        for kind, value, matched in (
            ("HIGH", bars[i].high, all(bars[i].high > b.high for b in neighbours)),
            ("LOW", bars[i].low, all(bars[i].low < b.low for b in neighbours)),
        ):
            if not matched:
                continue
            old = previous.get(kind)
            label = (
                ""
                if old is None
                else (
                    ("HH" if value > old else "LH")
                    if kind == "HIGH"
                    else ("HL" if value > old else "LL")
                )
            )
            result.append(
                Pivot(
                    index=i,
                    confirmed_index=i + length,
                    price=value,
                    kind=cast(PivotKind, kind),
                    label=label,
                    timestamp=bars[i].close_time,
                    confirmed_at=bars[i + length].close_time,
                )
            )
            previous[kind] = value
    return result


def successful_retest(
    bars: list[Candle], event: StructureBreak, tolerance: Decimal
) -> bool:
    """A retest must follow a break and cannot survive a close back through its level."""
    if event.index < 0 or len(bars) - 1 <= event.index:
        return False
    after = bars[event.index + 1 :]
    last = after[-1]
    if event.direction == Direction.LONG:
        return (
            all(b.close >= event.level - tolerance for b in after)
            and last.low <= event.level + tolerance
            and last.close > event.level
            and last.close > last.open
        )
    return (
        all(b.close <= event.level + tolerance for b in after)
        and last.high >= event.level - tolerance
        and last.close < event.level
        and last.close < last.open
    )


def structure(
    bars: list[Candle],
    length: int,
    tolerance: Decimal,
    require_close: bool = True,
    previous: Structure | None = None,
) -> Structure:
    """Advance a causal checkpoint so sliding history cannot relabel observed breaks.

    Pivot prices become usable at right-side confirmation time. Historical labels and
    breaks in the checkpoint are immutable; only newly closed bars advance the state.
    """
    if not bars:
        raise ValueError("Structure requires candles")
    points = pivots(bars, length)
    indexes = {bar.close_time: i for i, bar in enumerate(bars)}
    prior = (
        previous
        if previous is not None and previous.last_processed_at is not None
        else None
    )
    if prior is not None and prior.last_processed_at not in indexes:
        raise ValueError(
            "Structure checkpoint outside supplied history; backfill required"
        )
    result = Structure()
    active: dict[str, Pivot] = {}
    consumed: set[str] = set()
    start_at: datetime | None = None
    if prior is not None:
        start_at = prior.last_processed_at
        result.trend = prior.trend
        active = {k: p.model_copy(deep=True) for k, p in prior.active_pivots.items()}
        consumed = set(prior.consumed_kinds)
        result.pivots = [
            p.model_copy(
                update={
                    "index": indexes[p.timestamp],
                    "confirmed_index": indexes.get(p.confirmed_at, -1)
                    if p.confirmed_at is not None
                    else -1,
                }
            )
            for p in prior.pivots
            if p.timestamp in indexes
        ]
        old_breaks = [e for e in prior.breaks if e.timestamp in indexes]
        if not old_breaks and prior.breaks:
            old_breaks = prior.breaks[-1:]
        result.breaks = [
            e.model_copy(update={"index": indexes.get(e.timestamp, -1)})
            for e in old_breaks
        ]
    by_confirmation: dict[int, list[Pivot]] = {}
    for pivot in points:
        if start_at is None or (
            pivot.confirmed_at is not None and pivot.confirmed_at > start_at
        ):
            by_confirmation.setdefault(pivot.confirmed_index, []).append(pivot)
    for i, bar in enumerate(bars):
        if start_at is not None and bar.close_time <= start_at:
            continue
        for pivot in by_confirmation.get(i, []):
            old = active.get(pivot.kind)
            pivot.label = (
                ""
                if old is None
                else (
                    ("HH" if pivot.price > old.price else "LH")
                    if pivot.kind == "HIGH"
                    else ("HL" if pivot.price > old.price else "LL")
                )
            )
            active[pivot.kind] = pivot
            consumed.discard(pivot.kind)
            result.pivots.append(pivot)
        high, low = active.get("HIGH"), active.get("LOW")
        if result.trend == "MIXED" and high and low:
            if high.label == "HH" and low.label == "HL":
                result.trend = "BULLISH"
            elif high.label == "LH" and low.label == "LL":
                result.trend = "BEARISH"
        candidates = []
        for kind, direction, trend, value in (
            (
                "HIGH",
                Direction.LONG,
                "BULLISH",
                bar.close if require_close else bar.high,
            ),
            (
                "LOW",
                Direction.SHORT,
                "BEARISH",
                bar.close if require_close else bar.low,
            ),
        ):
            anchor = active.get(kind)
            if anchor is None or kind in consumed:
                continue
            crossed = value > anchor.price if kind == "HIGH" else value < anchor.price
            if crossed:
                candidates.append((kind, direction, trend, anchor))
        # An outside wick candle gives no ordering of the two breaks. Do not invent it.
        if len(candidates) == 1:
            kind, direction, trend, pivot = candidates[0]
            event_kind: BreakKind = (
                "INITIAL_BREAK"
                if result.trend == "MIXED"
                else "BOS"
                if result.trend == trend
                else "CHoCH"
            )
            result.breaks.append(
                StructureBreak(
                    index=i,
                    timestamp=bar.close_time,
                    level=pivot.price,
                    direction=direction,
                    kind=event_kind,
                )
            )
            result.trend = trend
            consumed.add(kind)
    if result.breaks and successful_retest(bars, result.breaks[-1], tolerance):
        result.retest = result.breaks[-1].direction
    last = bars[-1]
    highs = [
        p
        for p in result.pivots
        if p.kind == "HIGH"
        and p.confirmed_at is not None
        and p.confirmed_at < last.close_time
    ]
    lows = [
        p
        for p in result.pivots
        if p.kind == "LOW"
        and p.confirmed_at is not None
        and p.confirmed_at < last.close_time
    ]
    swept_low = bool(
        lows and "LOW" not in consumed and last.low < lows[-1].price < last.close
    )
    swept_high = bool(
        highs and "HIGH" not in consumed and last.high > highs[-1].price > last.close
    )
    if swept_low != swept_high:
        result.sweep = Direction.LONG if swept_low else Direction.SHORT
    result.equal_highs = (
        len(highs) >= 2 and abs(highs[-1].price - highs[-2].price) <= tolerance
    )
    result.equal_lows = (
        len(lows) >= 2 and abs(lows[-1].price - lows[-2].price) <= tolerance
    )
    result.active_pivots = active
    result.consumed_kinds = sorted(consumed)
    result.last_processed_at = last.close_time
    return result
