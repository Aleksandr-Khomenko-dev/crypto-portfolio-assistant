"""Closed-candle structural context for the early engine (pure).

Zones, ATR, 5m structure and patterns are derived ONLY from closed candles through the
normal analytics (`analyze_frame`, `zones`, `structure`, `pivots`). Nothing here reads
an unfinished candle; realtime prices are applied later, in the engine, as context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.early.model import EarlyConfig, Extension, KeyZone, PatternCandidate
from app.early.patterns import detect_patterns
from app.scanner.domain import Candle, FrameAnalysis, Pivot, StructureBreak

HTF_ORDER = ("4h", "1h", "15m")
MAJORS = ("BTCUSDT", "ETHUSDT")


@dataclass
class StructureContext:
    symbol: str
    built_at: datetime  # close time of the latest CLOSED 5m candle used
    atr: dict[str, float]  # per timeframe, closed candles only
    zones: list[KeyZone]
    bars5: list[Candle]
    bars15: list[Candle] = field(default_factory=list)
    breaks5: list[StructureBreak] = field(default_factory=list)
    pivots5: list[Pivot] = field(default_factory=list)
    rvol5: float | None = None
    trend: dict[str, str] = field(default_factory=dict)
    patterns: list[PatternCandidate] = field(default_factory=list)
    oi_change_15m: float | None = None  # stored boundary snapshots; None = нет данных
    funding_rate: float | None = None  # fraction per funding interval
    funding_state: str | None = None
    market: dict[str, str] = field(default_factory=dict)  # BTC/ETH bias, never self
    spread_pct: float | None = None

    def ref_atr(self, timeframe: str) -> float:
        """ATR used to normalise distances to a zone of `timeframe`."""
        if timeframe in ("1h", "4h"):
            return self.atr.get("1h") or self.atr.get("4h") or 0.0
        return self.atr.get(timeframe) or self.atr.get("15m") or 0.0


def key_zones(
    frames: dict[str, FrameAnalysis], atr: dict[str, float], cfg: EarlyConfig
) -> list[KeyZone]:
    """Nearest meaningful S/R zones and previous breakout levels, higher timeframe
    first; a lower-timeframe zone overlapping a higher one is dropped (no duplicates)."""
    chosen: list[KeyZone] = []

    def ref(tf: str) -> float:
        return atr.get("1h", 0.0) if tf in ("1h", "4h") else atr.get(tf, 0.0)

    def overlaps(lower: float, upper: float) -> bool:
        return any(z.lower <= upper and lower <= z.upper for z in chosen)

    for tf in HTF_ORDER:
        frame = frames.get(tf)
        if frame is None or ref(tf) <= 0:
            continue
        for kind, zones in (
            ("SUPPORT", frame.supports),
            ("RESISTANCE", frame.resistances),
        ):
            for zone in zones[: cfg.zones_per_side]:
                lower, upper = float(zone.lower), float(zone.upper)
                if zone.interactions < cfg.zone_min_touches or overlaps(lower, upper):
                    continue
                chosen.append(
                    KeyZone(
                        tf,
                        kind,
                        lower,
                        upper,
                        zone.created_at,
                        zone.interactions,
                        ref(tf),
                    )
                )
        if tf == "15m":
            continue  # only 1H/4H previous breakout levels are "important"
        price = float(frame.candle.close)
        width = frame.technical.atr * cfg.zone_width_atr
        for event in frame.macro.breaks[-2:]:
            level = float(event.level)
            lower, upper = level - width, level + width
            if overlaps(lower, upper):
                continue
            chosen.append(
                KeyZone(
                    tf,
                    "SUPPORT" if price >= level else "RESISTANCE",
                    lower,
                    upper,
                    event.timestamp,
                    1,
                    ref(tf),
                    "BREAK_LEVEL",
                )
            )
    return chosen


def asset_bias(frames: dict[str, FrameAnalysis]) -> str:
    """Same vote as the scanner's market context (completed 1h and 4h only)."""
    votes = 0
    for tf in ("1h", "4h"):
        frame = frames.get(tf)
        if frame is None:
            return "нет данных"
        t = frame.technical
        votes += (
            1
            if frame.macro.trend == t.alignment == "BULLISH" and t.momentum_pct > 0
            else -1
            if frame.macro.trend == t.alignment == "BEARISH" and t.momentum_pct < 0
            else 0
        )
    return "bullish" if votes >= 2 else "bearish" if votes <= -2 else "neutral"


def market_bias(
    symbol: str, majors: dict[str, dict[str, FrameAnalysis]]
) -> dict[str, str]:
    """BTC/ETH context for `symbol`, never comparing a major against itself."""
    return {
        major.removesuffix("USDT"): asset_bias(majors[major])
        for major in MAJORS
        if major != symbol and major in majors
    }


def extension(
    ctx: StructureContext, price: float, direction: str, cfg: EarlyConfig
) -> Extension | None:
    """How far price already travelled from the move's origin (lowest low for LONG,
    highest high for SHORT) over the last closed 5m bars, in 1H ATR and percent.
    A level the market has accepted (time at price) resets the origin window."""
    bars = ctx.bars5[-cfg.late_origin_lookback_bars :]
    atr = ctx.ref_atr("1h")
    if not bars or atr <= 0 or price <= 0:
        return None
    band = cfg.late_acceptance_atr * atr
    accepted = [
        i
        for i, b in enumerate(bars)
        if float(b.low) <= price + band and float(b.high) >= price - band
    ]
    if len(accepted) >= cfg.late_acceptance_bars:
        bars = bars[accepted[0] :]  # price is accepted here: measure from that base
    if direction == "LONG":
        origin_bar = min(bars, key=lambda b: b.low)
        origin = float(origin_bar.low)
        travelled = price - origin
    else:
        origin_bar = max(bars, key=lambda b: b.high)
        origin = float(origin_bar.high)
        travelled = origin - price
    distance = travelled / atr
    return Extension(
        origin_price=origin,
        origin_at=origin_bar.close_time,
        move_pct=round((price / origin - 1) * 100, 2),
        distance_atr=round(distance, 2),
        late=distance > cfg.late_origin_atr,
    )


def build_context(
    symbol: str,
    frames: dict[str, FrameAnalysis],
    five: FrameAnalysis,
    bars5: list[Candle],
    bars15: list[Candle],
    cfg: EarlyConfig,
    *,
    majors: dict[str, dict[str, FrameAnalysis]] | None = None,
    oi_change_15m: float | None = None,
    funding_rate: float | None = None,
    funding_state: str | None = None,
    spread_pct: float | None = None,
) -> StructureContext:
    closed5 = [b for b in bars5 if b.close_time <= five.candle.close_time]
    atr = {"5m": five.technical.atr}
    atr.update({tf: f.technical.atr for tf, f in frames.items()})
    zones = key_zones(frames, atr, cfg)
    ctx = StructureContext(
        symbol=symbol,
        built_at=five.candle.close_time,
        atr=atr,
        zones=zones,
        bars5=closed5,
        bars15=bars15,
        breaks5=list(five.micro.breaks),
        pivots5=list(five.micro.pivots),
        rvol5=five.technical.rvol,
        trend={tf: f.macro.trend for tf, f in frames.items()},
        oi_change_15m=oi_change_15m,
        funding_rate=funding_rate,
        funding_state=funding_state,
        market=market_bias(symbol, majors or {}),
        spread_pct=spread_pct,
    )
    ctx.patterns = detect_patterns(closed5, "5m", atr["5m"], zones, cfg)
    if bars15 and "15m" in atr:
        ctx.patterns += detect_patterns(bars15, "15m", atr["15m"], zones, cfg)
    return ctx
