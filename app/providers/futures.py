from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.scanner.domain import Candle, Contract, Derivatives, Ticker, Timeframe


class FuturesProvider(Protocol):
    """Public data only. Implementations never accept credentials or execute orders."""

    async def contracts(self) -> list[Contract]: ...
    async def tickers(self) -> dict[str, Ticker]: ...
    async def candles(
        self, symbol: str, timeframe: Timeframe, now: datetime
    ) -> list[Candle]: ...
    async def derivatives(self, symbol: str, now: datetime) -> Derivatives: ...
    async def aclose(self) -> None: ...
