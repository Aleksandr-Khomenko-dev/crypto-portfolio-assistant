from __future__ import annotations

from app.config import Settings, get_settings
from app.providers.binance import BinanceMarketProvider
from app.providers.coingecko import CoinGeckoMarketProvider
from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote


class FallbackMarketDataProvider(AbstractMarketDataProvider):
    provider_name = "fallback"

    def __init__(self, providers: list[AbstractMarketDataProvider]) -> None:
        self.providers = providers

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        quotes = await self.fetch_quotes([query])
        return quotes.get(query.asset_id)

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict:
        pending = {query.asset_id: query for query in queries}
        resolved: dict = {}

        for provider in self.providers:
            if not pending:
                break
            provider_quotes = await provider.fetch_quotes(list(pending.values()))
            resolved.update(provider_quotes)
            for asset_id in provider_quotes:
                pending.pop(asset_id, None)

        return resolved

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()


def create_market_provider(settings: Settings | None = None) -> AbstractMarketDataProvider:
    settings = settings or get_settings()
    providers: list[AbstractMarketDataProvider] = []

    for provider_name in settings.provider_order:
        if provider_name == "coingecko":
            providers.append(CoinGeckoMarketProvider(settings=settings))
        elif provider_name == "binance":
            providers.append(BinanceMarketProvider(settings=settings))

    return FallbackMarketDataProvider(providers)
