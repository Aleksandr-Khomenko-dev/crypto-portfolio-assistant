from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.scanner.domain import Direction, FrameAnalysis, RiskPlan


def risk_plan(
    direction: Direction, frames: dict[str, FrameAnalysis], settings: Settings
) -> RiskPlan:
    frame = frames["15m"]
    price = frame.candle.close
    atr = Decimal(str(frame.technical.atr))
    supports = [z for f in frames.values() for z in f.supports if z.lower < price]
    resistances = [z for f in frames.values() for z in f.resistances if z.upper > price]
    if direction == Direction.LONG:
        stops = sorted(supports, key=lambda z: z.lower, reverse=True)
        targets = sorted(resistances, key=lambda z: z.lower)
        invalidation = (
            stops[0].lower - atr * settings.scanner_stop_buffer_atr if stops else None
        )
        obstacle = targets[0].lower if targets else None
    else:
        stops = sorted(resistances, key=lambda z: z.upper)
        targets = sorted(supports, key=lambda z: z.upper, reverse=True)
        invalidation = (
            stops[0].upper + atr * settings.scanner_stop_buffer_atr if stops else None
        )
        obstacle = targets[0].upper if targets else None
    result = RiskPlan(
        invalidation=invalidation, nearest_obstacle=obstacle, targets=targets[:3]
    )
    if invalidation is None or invalidation <= 0:
        return result
    distance = (
        price - invalidation if direction == Direction.LONG else invalidation - price
    )
    if distance <= 0:
        return result
    result.stop_distance, result.stop_atr = distance, float(distance / atr)
    if obstacle is None:
        result.reason = "NO_OBSERVED_TARGET"
        return result
    room = obstacle - price if direction == Direction.LONG else price - obstacle
    result.rr = float(max(room, Decimal(0)) / distance)
    if room <= 0 or result.rr < float(settings.scanner_min_rr):
        result.reason = "NO_ROOM"
    elif frame.technical.extension_atr > settings.scanner_max_extension_atr:
        result.reason = "OVEREXTENDED"
    else:
        result.valid, result.reason = True, "VALID"
    return result
