"""Deterministic formation candidates on CLOSED 5m/15m candles (pure).

Six high-value structures only: compression at a level, ascending / descending
triangle, tight continuation flag, liquidity sweep + reclaim, breakout compression.
A candidate is reported BEFORE completion (no breakout yet); `formation_strength` is
a 0-100 diagnostic of how well-formed it is, never a success probability.
"""

from __future__ import annotations

from itertools import pairwise
from statistics import mean

from app.analytics.structure import pivots
from app.analytics.technical import true_ranges
from app.early.model import EarlyConfig, KeyZone, PatternCandidate
from app.scanner.domain import Candle

PATTERN_TYPES = (
    "COMPRESSION_AT_LEVEL",
    "ASCENDING_TRIANGLE",
    "DESCENDING_TRIANGLE",
    "BULL_FLAG",
    "BEAR_FLAG",
    "SWEEP_RECLAIM",
    "BREAKOUT_COMPRESSION",
)


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


def compression_ratio(bars: list[Candle], size: int = 8) -> float | None:
    """Mean true range of the last `size` bars over the first `size` of the window."""
    if len(bars) < 2 * size:
        return None
    ranges = true_ranges(bars)
    early = mean(ranges[:size])
    return round(mean(ranges[-size:]) / early, 3) if early > 0 else None


def _triangles(
    window: list[Candle], atr: float, tf: str, cfg: EarlyConfig
) -> list[PatternCandidate]:
    points = pivots(window, 2)  # each pivot confirmed by 2 later CLOSED bars
    highs = [p for p in points if p.kind == "HIGH"][-4:]
    lows = [p for p in points if p.kind == "LOW"][-4:]
    tol = cfg.pattern_tolerance_atr * atr
    last = float(window[-1].close)
    ratio = compression_ratio(window)
    found = []
    for direction in ("LONG", "SHORT"):
        flat_side, sloped_side = (highs, lows) if direction == "LONG" else (lows, highs)
        if len(flat_side) < 2 or len(sloped_side) < 2:
            continue
        prices = [float(p.price) for p in flat_side]
        edge = max(prices) if direction == "LONG" else min(prices)
        flat = [p for p in flat_side if abs(float(p.price) - edge) <= tol]
        if len(flat) < 2:
            continue
        slope = [p for p in sloped_side if p.index > flat[0].index - 3][-3:]
        values = [float(p.price) for p in slope]
        converging = len(values) >= 2 and all(
            (b > a) if direction == "LONG" else (b < a) for a, b in pairwise(values)
        )
        distance = (edge - last) / atr if direction == "LONG" else (last - edge) / atr
        if (
            not converging
            or not 0 <= distance <= cfg.pattern_level_atr
            or (ratio is not None and ratio > 1.0)
        ):
            continue
        strength = (
            20 * len(flat)
            + 10 * len(slope)
            + 40 * max(0.0, 1 - (ratio or 1.0))
            + 20 * max(0.0, 1 - distance / cfg.pattern_level_atr)
        )
        started = window[min(flat[0].index, slope[0].index)].open_time
        found.append(
            PatternCandidate(
                pattern_type="ASCENDING_TRIANGLE"
                if direction == "LONG"
                else "DESCENDING_TRIANGLE",
                direction=direction,
                source_timeframe=tf,
                formation_started_at=started,
                boundary_level=edge,
                secondary_boundary=values[-1],
                touch_count=len(flat),
                compression_ratio=ratio,
                distance_to_trigger_atr=round(distance, 3),
                formation_strength=_clamp(strength),
                invalidation_condition=(
                    f"{tf} close below the last higher low"
                    if direction == "LONG"
                    else f"{tf} close above the last lower high"
                ),
                invalidation_level=values[-1],
                atr=atr,
                evidence=[
                    f"{len(flat)} touches of the flat boundary",
                    "higher lows" if direction == "LONG" else "lower highs",
                ],
            )
        )
    return found


def _flags(
    window: list[Candle], atr: float, tf: str, cfg: EarlyConfig
) -> list[PatternCandidate]:
    found = []
    n = len(window)
    for direction in ("LONG", "SHORT"):
        best = None
        for peak in range(n - 6, max(10, n - 21) - 1, -1):
            leg = window[peak - 10 : peak + 1]
            flag = window[peak + 1 :]
            if direction == "LONG":
                start = min(leg, key=lambda b: b.low)
                impulse = float(window[peak].high - start.low)
                top = max(float(b.high) for b in flag)
                bottom = min(float(b.low) for b in flag)
                retrace = (
                    (float(window[peak].high) - bottom) / impulse if impulse else 1
                )
                capped = top <= float(window[peak].high) + 0.25 * atr
            else:
                start = max(leg, key=lambda b: b.high)
                impulse = float(start.high - window[peak].low)
                top = max(float(b.high) for b in flag)
                bottom = min(float(b.low) for b in flag)
                retrace = (top - float(window[peak].low)) / impulse if impulse else 1
                capped = bottom >= float(window[peak].low) - 0.25 * atr
            if impulse < cfg.flag_impulse_atr * atr or not capped or retrace > 0.5:
                continue
            ranges = true_ranges(window)
            leg_tr = mean(ranges[peak - 10 : peak + 1])
            flag_tr = mean(ranges[peak + 1 :])
            ratio = flag_tr / leg_tr if leg_tr else 1.0
            if ratio > 0.7 or top - bottom > 0.5 * impulse:
                continue
            best = (peak, start, impulse, top, bottom, ratio, retrace)
            break
        if best is None:
            continue
        peak, start, impulse, top, bottom, ratio, retrace = best
        last = float(window[-1].close)
        edge, other = (top, bottom) if direction == "LONG" else (bottom, top)
        distance = abs(edge - last) / atr
        strength = (
            30
            + 30 * max(0.0, 1 - ratio / 0.7)
            + 20 * max(0.0, 1 - retrace / 0.5)
            + 20 * max(0.0, 1 - distance / 1.5)
        )
        found.append(
            PatternCandidate(
                pattern_type="BULL_FLAG" if direction == "LONG" else "BEAR_FLAG",
                direction=direction,
                source_timeframe=tf,
                formation_started_at=start.open_time,
                boundary_level=edge,
                secondary_boundary=other,
                touch_count=n - peak - 1,
                compression_ratio=round(ratio, 3),
                distance_to_trigger_atr=round(distance, 3),
                formation_strength=_clamp(strength),
                invalidation_condition=(
                    f"{tf} close below the flag low"
                    if direction == "LONG"
                    else f"{tf} close above the flag high"
                ),
                invalidation_level=other,
                atr=atr,
                evidence=[
                    f"impulse {impulse / atr:.1f} ATR",
                    f"retrace {retrace * 100:.0f}%",
                ],
            )
        )
    return found


def _zone_patterns(
    window: list[Candle],
    atr: float,
    tf: str,
    zones: list[KeyZone],
    cfg: EarlyConfig,
) -> list[PatternCandidate]:
    found: dict[tuple[str, str], PatternCandidate] = {}
    tol = cfg.pattern_tolerance_atr * atr
    last = window[-1]
    close = float(last.close)
    recent = window[-10:]
    high = max(float(b.high) for b in recent)
    low = min(float(b.low) for b in recent)
    ratio = compression_ratio(window[-16:])
    compressed = (
        ratio is not None
        and ratio <= cfg.pattern_compression_ratio
        and high - low <= 1.5 * atr
    )
    for zone in zones:
        # Liquidity sweep + reclaim within the last 3 closed bars.
        tail = window[-4:]
        if zone.kind == "SUPPORT":
            swept = [b for b in tail[1:] if float(b.low) < zone.lower - 0.05 * atr]
            if swept and float(tail[0].close) >= zone.lower and close > zone.lower:
                extreme = min(float(b.low) for b in swept)
                found.setdefault(
                    ("SWEEP_RECLAIM", "LONG"),
                    _candidate(
                        "SWEEP_RECLAIM",
                        "LONG",
                        tf,
                        swept[0],
                        zone.upper,
                        extreme,
                        zone,
                        None,
                        max(0.0, zone.upper - close) / atr,
                        atr,
                        45
                        + 10 * min(zone.touches, 3)
                        + (15 if close > zone.upper else 0),
                        f"{tf} close below the sweep low",
                        ["sweep below support", "close back above"],
                    ),
                )
        else:
            swept = [b for b in tail[1:] if float(b.high) > zone.upper + 0.05 * atr]
            if swept and float(tail[0].close) <= zone.upper and close < zone.upper:
                extreme = max(float(b.high) for b in swept)
                found.setdefault(
                    ("SWEEP_RECLAIM", "SHORT"),
                    _candidate(
                        "SWEEP_RECLAIM",
                        "SHORT",
                        tf,
                        swept[0],
                        zone.lower,
                        extreme,
                        zone,
                        None,
                        max(0.0, close - zone.lower) / atr,
                        atr,
                        45
                        + 10 * min(zone.touches, 3)
                        + (15 if close < zone.lower else 0),
                        f"{tf} close above the sweep high",
                        ["sweep above resistance", "close back below"],
                    ),
                )
        if not compressed:
            continue
        assert ratio is not None
        base = (
            40
            + 40 * (1 - ratio / cfg.pattern_compression_ratio)
            + 5 * min(zone.touches, 4)
        )
        pressing_up = (
            zone.kind == "RESISTANCE"
            and abs(zone.lower - high) <= tol
            and close < zone.lower
        )
        pressing_down = (
            zone.kind == "SUPPORT"
            and abs(low - zone.upper) <= tol
            and close > zone.upper
        )
        if pressing_up or pressing_down:
            direction = "LONG" if pressing_up else "SHORT"
            edge = zone.upper if pressing_up else zone.lower
            found.setdefault(
                ("BREAKOUT_COMPRESSION", direction),
                _candidate(
                    "BREAKOUT_COMPRESSION",
                    direction,
                    tf,
                    recent[0],
                    edge,
                    low if pressing_up else high,
                    zone,
                    ratio,
                    abs(edge - close) / atr,
                    atr,
                    base,
                    f"{tf} close back below the compression low"
                    if pressing_up
                    else f"{tf} close back above the compression high",
                    ["range compresses into the level"],
                ),
            )
        elif low <= zone.upper + tol and high >= zone.lower - tol:
            direction = "LONG" if zone.kind == "SUPPORT" else "SHORT"
            edge = zone.upper if direction == "LONG" else zone.lower
            found.setdefault(
                ("COMPRESSION_AT_LEVEL", direction),
                _candidate(
                    "COMPRESSION_AT_LEVEL",
                    direction,
                    tf,
                    recent[0],
                    high if direction == "LONG" else low,
                    edge,
                    zone,
                    ratio,
                    abs((high if direction == "LONG" else low) - close) / atr,
                    atr,
                    base,
                    f"{tf} close beyond the zone ({'below' if direction == 'LONG' else 'above'})",
                    ["range compresses inside the zone"],
                    invalidation=zone.lower if direction == "LONG" else zone.upper,
                ),
            )
    return list(found.values())


def _candidate(
    kind,
    direction,
    tf,
    first_bar,
    boundary,
    secondary,
    zone,
    ratio,
    distance,
    atr,
    strength,
    condition,
    evidence,
    invalidation=None,
) -> PatternCandidate:
    return PatternCandidate(
        pattern_type=kind,
        direction=direction,
        source_timeframe=tf,
        formation_started_at=first_bar.open_time,
        boundary_level=boundary,
        secondary_boundary=secondary,
        touch_count=zone.touches,
        compression_ratio=ratio,
        distance_to_trigger_atr=round(distance, 3),
        formation_strength=_clamp(strength),
        invalidation_condition=condition,
        invalidation_level=secondary if invalidation is None else invalidation,
        atr=atr,
        evidence=[*evidence, f"{zone.timeframe} {zone.kind.lower()} zone"],
    )


def detect_patterns(
    bars: list[Candle],
    timeframe: str,
    atr: float,
    zones: list[KeyZone],
    cfg: EarlyConfig,
) -> list[PatternCandidate]:
    """Candidates on the most recent closed bars; nothing is confirmed here."""
    window = bars[-cfg.pattern_lookback_bars :]
    if len(window) < 20 or atr <= 0:
        return []
    return [
        *_triangles(window, atr, timeframe, cfg),
        *_flags(window, atr, timeframe, cfg),
        *_zone_patterns(window, atr, timeframe, zones, cfg),
    ]
