from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote


class MockMarketProvider(AbstractMarketDataProvider):
    provider_name = "mock"

    def __init__(self, overrides: dict[str, Decimal] | None = None) -> None:
        self.overrides = overrides or {}

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        price = self.overrides.get(query.symbol.upper(), Decimal("100"))
        return MarketQuote(
            asset_id=query.asset_id,
            provider=self.provider_name,
            symbol=query.symbol.upper(),
            price_usd=price,
            market_cap_usd=Decimal("1000000000"),
            volume_24h_usd=Decimal("25000000"),
            change_1h_pct=Decimal("2.0"),
            change_24h_pct=Decimal("8.0"),
            source_timestamp=datetime.now(timezone.utc),
            raw={"symbol": query.symbol.upper()},
        )

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        quotes: dict[UUID, MarketQuote] = {}
        for query in queries:
            quote = await self.fetch_quote(query)
            if quote is not None:
                quotes[query.asset_id] = quote
        return quotes
