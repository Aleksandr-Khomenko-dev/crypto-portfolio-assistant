from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.scanner.domain import Candle, Contract, Derivatives, Ticker, Timeframe


class FuturesProvider(Protocol):
    """Public data only. Implementations never accept credentials or execute orders.

    Symbols are canonical (``BTCUSDT``); each adapter converts to its exchange format.
    """

    exchange: str  # e.g. "BINGX"; stored on results, setups and outcomes
    # False: the exchange has no public OI history, so the scanner records its own.
    publishes_oi_history: bool

    async def contracts(self) -> list[Contract]: ...
    async def tickers(self) -> dict[str, Ticker]: ...
    async def candles(
        self, symbol: str, timeframe: Timeframe, now: datetime
    ) -> list[Candle]: ...
    async def range_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]: ...
    async def derivatives(self, symbol: str, now: datetime) -> Derivatives: ...
    async def aclose(self) -> None: ...
