from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AssetMarketQuery:
    asset_id: UUID
    symbol: str
    name: str | None = None
    coingecko_id: str | None = None
    binance_symbol: str | None = None
    bybit_symbol: str | None = None


@dataclass(slots=True)
class MarketQuote:
    asset_id: UUID
    provider: str
    symbol: str
    price_usd: Decimal
    market_cap_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    change_1h_pct: Decimal | None = None
    change_24h_pct: Decimal | None = None
    source_timestamp: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class AbstractMarketDataProvider(ABC):
    provider_name: str

    @abstractmethod
    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        raise NotImplementedError

    @abstractmethod
    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
