from __future__ import annotations

from typing import Protocol

from app.config import Settings
from app.scanner.domain import FrameAnalysis, MarketContext


class SupplementalContextProvider(Protocol):
    """Future on-chain, macro or news adapters must disclose missing coverage."""

    async def context(self, symbol: str) -> MarketContext: ...


class UnavailableContextProvider:
    async def context(self, symbol: str) -> MarketContext:
        return MarketContext(
            explanation="No on-chain, macro or news provider configured"
        )


def market_context(
    assets: dict[str, dict[str, FrameAnalysis]], settings: Settings
) -> MarketContext:
    if not all(s in assets for s in ("BTCUSDT", "ETHUSDT")):
        return MarketContext()
    result = MarketContext(
        state="MIXED", explanation="BTC/ETH completed 1h and 4h context"
    )
    votes = []
    high_volatility = False
    for symbol, frames in assets.items():
        row: dict[str, str | float] = {}
        for tf in ("1h", "4h"):
            frame = frames[tf]
            technical = frame.technical
            row[tf + "_structure"] = frame.macro.trend
            row[tf + "_ema"] = technical.alignment
            row[tf + "_momentum_pct"] = technical.momentum_pct
            row[tf + "_atr_pct"] = technical.atr_pct
            votes.append(
                1
                if frame.macro.trend == technical.alignment == "BULLISH"
                and technical.momentum_pct > 0
                else -1
                if frame.macro.trend == technical.alignment == "BEARISH"
                and technical.momentum_pct < 0
                else 0
            )
            high_volatility |= (
                technical.atr_pct >= settings.scanner_high_volatility_atr_pct
            )
        result.assets[symbol] = row
    btc = assets["BTCUSDT"]["1h"]
    recent_break = btc.macro.breaks[-1] if btc.macro.breaks else None
    risk_off = (
        recent_break is not None
        and recent_break.direction == "SHORT"
        and (btc.candle.close_time - recent_break.timestamp).total_seconds()
        <= settings.scanner_event_max_bars * 3600
        and btc.technical.adx >= settings.scanner_adx_min
        and btc.technical.momentum_pct < 0
    )
    result.state = (
        "RISK_OFF"
        if risk_off
        else "HIGH_VOLATILITY"
        if high_volatility
        else (
            "BULLISH" if sum(votes) >= 3 else "BEARISH" if sum(votes) <= -3 else "MIXED"
        )
    )
    return result
