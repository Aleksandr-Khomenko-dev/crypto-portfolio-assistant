"""Early trend ignition near a higher-timeframe zone (pure, closed 5m candles).

Deterministic confluence at a 1H/4H zone: a FRESH structural shift (5m CHoCH, first
5m BOS, sweep + reclaim, or reclaim + higher low within the last 3 closed 5m bars)
is REQUIRED; the other factors add diagnostic
`ignition_strength` (0-100). Not every factor is needed, and the strength is not a
probability of success.
"""

from __future__ import annotations

from app.early.context import StructureContext
from app.early.model import EarlyConfig, EarlyEvent, EventType, KeyZone

# (code, weight, Russian explanation). Neutral context (funding OK, BTC/ETH not
# against, OI stable) is SHOWN but weighs 0: it cannot make a weak setup look strong.
Factor = tuple[str, int, str]


def _factors(
    ctx: StructureContext, zone: KeyZone, direction: str, cfg: EarlyConfig
) -> tuple[list[Factor], bool]:
    bars = ctx.bars5[-cfg.ignition_lookback_bars :]
    long = direction == "LONG"
    last = bars[-1]
    close = float(last.close)
    factors: list[Factor] = []
    edge = zone.upper if long else zone.lower
    beyond = [
        (float(b.close) > edge) if long else (float(b.close) < edge) for b in bars
    ]
    if long:
        swept = any(float(b.low) < zone.lower for b in bars) and close > zone.lower
    else:
        swept = any(float(b.high) > zone.upper for b in bars) and close < zone.upper
    # Reclaim is FRESH only: the first close back beyond the zone edge happened
    # within the last `ignition_fresh_bars` closed bars (no stale reclaims).
    reclaim = False
    if beyond[-1] and not all(beyond):
        last_inside = max(i for i, flag in enumerate(beyond) if not flag)
        reclaim = len(bars) - 1 - last_inside <= cfg.ignition_fresh_bars
    since = bars[-min(cfg.ignition_fresh_bars, len(bars))].open_time
    recent = [
        e for e in ctx.breaks5 if e.direction == direction and e.timestamp >= since
    ]
    choch = any(e.kind == "CHoCH" for e in recent)
    bos = any(e.kind in ("BOS", "INITIAL_BREAK") for e in recent)
    swing = "LOW" if long else "HIGH"
    confirmed = [
        p
        for p in ctx.pivots5
        if p.kind == swing
        and p.confirmed_at is not None
        and p.confirmed_at <= ctx.built_at
        and p.timestamp is not None
        and p.timestamp >= bars[0].open_time
    ]
    shifted_swing = bool(confirmed) and confirmed[-1].label == ("HL" if long else "LH")
    extremes = [float(b.low if long else b.high) for b in bars]
    decelerating = len(extremes) >= 9 and (
        min(extremes[-3:]) >= min(extremes[-9:-3])
        if long
        else max(extremes[-3:]) <= max(extremes[-9:-3])
    )
    word = "bullish" if long else "bearish"
    if swept:
        factors.append(("LIQUIDITY_SWEEP", 15, "снятие ликвидности за зоной и возврат"))
    if reclaim:
        factors.append(("ZONE_RECLAIM", 15, "произошёл reclaim зоны"))
    if choch:
        factors.append(("CHOCH_5M", 20, f"на 5m появился {word} CHoCH"))
    if bos:
        factors.append(("BOS_5M", 15, f"первый 5m BOS ({word})"))
    if shifted_swing:
        factors.append(
            (
                "HIGHER_LOW" if long else "LOWER_HIGH",
                15,
                "формируется higher low" if long else "формируется lower high",
            )
        )
    if decelerating:
        factors.append(
            (
                "MOMENTUM_DECELERATION",
                5,
                "продажи замедляются (нет нового минимума)"
                if long
                else "покупки замедляются (нет нового максимума)",
            )
        )
    if ctx.rvol5 is not None and ctx.rvol5 >= cfg.volume_confirm:
        factors.append(
            ("VOLUME_EXPANSION", 10, f"объём увеличивается (RVOL 5m {ctx.rvol5:.1f}x)")
        )
    oi = ctx.oi_change_15m
    if oi is not None:
        if oi >= 0.5:
            factors.append(("OI_EXPANSION", 10, f"OI растёт ({oi:+.2f}% за 15m)"))
        elif oi >= -0.2:
            factors.append(("OI_STABLE", 0, f"OI стабилизировался ({oi:+.2f}% за 15m)"))
    crowded = "CROWDED_LONG" if long else "CROWDED_SHORT"
    if ctx.funding_state and ctx.funding_state not in ("unavailable", crowded):
        factors.append(("FUNDING_OK", 0, "funding нейтральный/благоприятный"))
    against = "bearish" if long else "bullish"
    known = [v for v in ctx.market.values() if v in ("bullish", "bearish", "neutral")]
    if known and against not in known:
        factors.append(("MARKET_COMPATIBLE", 0, "BTC/ETH context не против"))
    shift = choch or bos or (swept and reclaim) or (shifted_swing and reclaim)
    # Detection records any fresh shift at a 1H/4H zone; whether it is strong enough
    # to TELL the user (volume/OI/strong reclaim) is the notification policy's job.
    return factors, shift


def ignition(
    ctx: StructureContext, direction: str, price: float, now: float, cfg: EarlyConfig
) -> EarlyEvent | None:
    """Strongest qualifying ignition for `direction` at the nearest touched zone."""
    if not ctx.bars5:
        return None
    long = direction == "LONG"
    kind = "SUPPORT" if long else "RESISTANCE"
    bars = ctx.bars5[-cfg.ignition_lookback_bars :]
    candidates = [
        z
        for z in ctx.zones
        if z.kind == kind and z.htf  # 1H/4H zones and breakout levels only
    ]
    candidates.sort(key=lambda z: abs((z.lower + z.upper) / 2 - price))
    for zone in candidates:
        ref = zone.atr
        if ref <= 0:
            continue
        if long:
            touched = (
                min(float(b.low) for b in bars)
                <= zone.upper + cfg.ignition_zone_atr * ref
            )
            distance = (price - zone.upper) / ref
            broken = price < zone.lower - cfg.fail_atr * ref
        else:
            touched = (
                max(float(b.high) for b in bars)
                >= zone.lower - cfg.ignition_zone_atr * ref
            )
            distance = (zone.lower - price) / ref
            broken = price > zone.upper + cfg.fail_atr * ref
        if not touched or broken or distance > cfg.ignition_max_distance_atr:
            continue
        factors, shift = _factors(ctx, zone, direction, cfg)
        strength = min(100, sum(weight for _, weight, _ in factors))
        if not shift or strength < cfg.ignition_min_strength:
            continue
        return EarlyEvent(
            symbol=ctx.symbol,
            event_type=EventType.BULLISH_IGNITION
            if long
            else EventType.BEARISH_IGNITION,
            direction=direction,
            price=price,
            detected_at=now,
            source_timeframe=zone.timeframe,
            zone=zone,
            strength=strength,
            reasons=[code for code, _, _ in factors],
            confirmations=[text for _, _, text in factors],
            metrics={"distance_to_zone_atr": round(max(distance, 0.0), 2)},
            key=zone.key,
        )
    return None
