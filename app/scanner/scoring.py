from __future__ import annotations

from datetime import datetime, timedelta

from app.config import Settings
from app.scanner.domain import (
    Derivatives,
    Direction,
    FrameAnalysis,
    MarketContext,
    Readiness,
    ScannerResult,
    Setup,
    SignalState,
    Ticker,
)
from app.scanner.risk import risk_plan

BLOCK_CAPS = {
    "regime": 15,
    "structure": 20,
    "location": 15,
    "trigger": 15,
    "volume_momentum": 10,
    "derivatives": 10,
    "market_context": 5,
    "risk_room": 10,
}


def signal_state(score: int, settings: Settings) -> SignalState:
    for threshold, state in (
        (settings.scanner_extreme_score, SignalState.EXTREME_CONFLUENCE),
        (settings.scanner_high_score, SignalState.HIGH_CONFLUENCE),
        (settings.scanner_setup_score, SignalState.SETUP_FORMING),
        (settings.scanner_watch_score, SignalState.WATCH),
    ):
        if score >= threshold:
            return state
    return SignalState.IGNORE


def score_setup(
    symbol: str,
    direction: Direction,
    frames: dict[str, FrameAnalysis],
    derivatives: Derivatives,
    context: MarketContext,
    settings: Settings,
) -> Setup:
    trend = "BULLISH" if direction == Direction.LONG else "BEARISH"
    hourly, high = frames["1h"], frames["4h"]
    t, price = hourly.technical, float(hourly.candle.close)
    blocks = dict.fromkeys(BLOCK_CAPS, 0)
    # Correlated observations share a capped block; BOS and CHoCH are alternatives.
    blocks["regime"] = (8 if high.technical.alignment == trend else 0) + (
        7 if hourly.technical.alignment == trend else 0
    )
    event = hourly.micro.breaks[-1] if hourly.micro.breaks else None
    recent = bool(
        event
        and event.direction == direction
        and event.kind != "INITIAL_BREAK"
        and (hourly.candle.close_time - event.timestamp).total_seconds()
        <= settings.scanner_event_max_bars * 3600
        and hourly.candle.close_time >= event.timestamp
    )
    blocks["structure"] = (10 if hourly.macro.trend == trend else 0) + (
        10 if recent else 5 if hourly.micro.trend == trend else 0
    )
    relevant = [
        z
        for frame in (hourly, high)
        for z in (frame.supports if direction == Direction.LONG else frame.resistances)
    ]
    near_zone = any(
        max(float(z.lower) - price, price - float(z.upper), 0) / t.atr
        <= settings.scanner_location_atr
        for z in relevant
        if (
            price >= float(z.lower)
            if direction == Direction.LONG
            else price <= float(z.upper)
        )
    )
    near_gap = any(
        g.direction == direction
        and g.status not in ("FILLED", "INVALIDATED")
        and (
            price >= float(g.lower)
            if direction == Direction.LONG
            else price <= float(g.upper)
        )
        and max(float(g.lower) - price, price - float(g.upper), 0) / t.atr
        <= settings.scanner_location_atr
        for frame in (hourly, high)
        for g in frame.fvgs
    )
    sweep = hourly.micro.sweep == direction
    location = near_zone or near_gap or sweep
    blocks["location"] = max(
        12 if near_zone else 0, 10 if near_gap else 0, 15 if sweep else 0
    )
    retest = recent and hourly.micro.retest == direction
    body = float(hourly.candle.close - hourly.candle.open) * (
        1 if direction == Direction.LONG else -1
    )
    wick = (
        float(min(hourly.candle.open, hourly.candle.close) - hourly.candle.low)
        if direction == Direction.LONG
        else float(hourly.candle.high - max(hourly.candle.open, hourly.candle.close))
    )
    rejection = body > 0 and wick > max(body, t.atr * 0.25)
    displacement = body >= t.atr
    blocks["trigger"] = max(
        # A reclaim/rejection candle used for sweep location is not a second trigger.
        15 if retest and not sweep else 0,
        8 if rejection and not sweep else 0,
        8
        if displacement
        and not sweep
        and not (event and event.timestamp == hourly.candle.close_time)
        else 0,
    )
    momentum = (
        (t.plus_di > t.minus_di and t.rsi >= 50)
        if direction == Direction.LONG
        else (t.minus_di > t.plus_di and t.rsi <= 50)
    )
    volume = t.rvol is not None and t.rvol >= settings.scanner_rvol_min
    blocks["volume_momentum"] = (5 if volume else 0) + (
        5 if momentum and t.adx >= settings.scanner_adx_min else 0
    )
    constructive = derivatives.interpretation == (
        "NEW_LONG_PARTICIPATION_POSSIBLE"
        if direction == Direction.LONG
        else "SHORT_BUILD_POSSIBLE"
    )
    crowded = derivatives.funding_state == (
        "CROWDED_LONG" if direction == Direction.LONG else "CROWDED_SHORT"
    )
    if settings.scanner_use_derivatives:
        blocks["derivatives"] = (6 if constructive else 0) + (
            4 if derivatives.funding_state != "unavailable" and not crowded else 0
        )
    if settings.scanner_use_btc_context and symbol not in ("BTCUSDT", "ETHUSDT"):
        blocks["market_context"] = (
            5 if context.state == trend else 2 if context.state == "MIXED" else 0
        )
        # Risk-off BTC caps alt LONG regime credit as well as withholding context points.
        if (
            direction == Direction.LONG
            and symbol not in ("BTCUSDT", "ETHUSDT")
            and context.state == "RISK_OFF"
        ):
            blocks["regime"] = min(blocks["regime"], 5)
    risk = risk_plan(direction, frames, settings)
    blocks["risk_room"] = 10 if risk.valid else 0
    if not location:
        readiness, message = (
            Readiness.WAIT_LOCATION,
            "Wait for support/resistance, imbalance or reclaimed sweep location",
        )
    elif high.technical.alignment != trend or hourly.macro.trend != trend or not recent:
        readiness, message = (
            Readiness.WAIT_STRUCTURE,
            "Wait for a closed 1h structural break aligned with 4h trend",
        )
    elif not retest:
        readiness, message = (
            Readiness.WAIT_RETEST,
            "Wait for a successful closed-candle retest",
        )
    elif not volume:
        readiness, message = (
            Readiness.WAIT_VOLUME,
            "Wait for completed-bar relative volume confirmation",
        )
    elif not risk.valid:
        readiness, message = (
            Readiness.WAIT_RISK,
            "Wait for acceptable structural risk and room: " + risk.reason,
        )
    else:
        readiness, message = (
            Readiness.READY,
            "Conditions met for review; informational setup only",
        )
    score = sum(min(BLOCK_CAPS[k], max(0, value)) for k, value in blocks.items())
    return Setup(
        symbol=symbol,
        direction=direction,
        score=score,
        state=signal_state(score, settings),
        readiness=readiness,
        next_condition=message,
        blocks=blocks,
        risk=risk,
    )


def build_result(
    symbol: str,
    frames: dict[str, FrameAnalysis],
    derivatives: Derivatives,
    context: MarketContext,
    ticker: Ticker,
    now: datetime,
    settings: Settings,
) -> ScannerResult:
    setups = [
        score_setup(symbol, direction, frames, derivatives, context, settings)
        for direction in Direction
    ]
    return ScannerResult(
        symbol=symbol,
        signal_timeframe="1h",
        price=frames["1h"].candle.close,
        observed_price=ticker.price,
        observed_at=ticker.timestamp,
        candle_closed_at=frames["1h"].candle.close_time,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.scanner_setup_expiry_minutes),
        long_score=setups[0].score,
        short_score=setups[1].score,
        setups=setups,
        frames=frames,
        derivatives=derivatives,
        context=context,
    )
