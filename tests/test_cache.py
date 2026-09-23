from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.providers.cached import CachedMarketDataProvider
from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote


class CountingProvider(AbstractMarketDataProvider):
    """Provider that counts how many times it was actually called."""

    provider_name = "counting"

    def __init__(self) -> None:
        self.call_count = 0

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        quotes = await self.fetch_quotes([query])
        return quotes.get(query.asset_id)

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict:
        self.call_count += 1
        return {
            q.asset_id: MarketQuote(
                asset_id=q.asset_id,
                provider=self.provider_name,
                symbol=q.symbol,
                price_usd=Decimal("100"),
            )
            for q in queries
        }


@pytest.mark.asyncio
async def test_cache_hit_on_second_call() -> None:
    inner = CountingProvider()
    cached = CachedMarketDataProvider(inner, ttl_seconds=60)

    asset_id = uuid4()
    query = AssetMarketQuery(asset_id=asset_id, symbol="BTC")

    await cached.fetch_quotes([query])
    await cached.fetch_quotes([query])

    assert inner.call_count == 1
    assert cached.cache_size == 1


@pytest.mark.asyncio
async def test_cache_miss_after_invalidation() -> None:
    inner = CountingProvider()
    cached = CachedMarketDataProvider(inner, ttl_seconds=60)

    asset_id = uuid4()
    query = AssetMarketQuery(asset_id=asset_id, symbol="ETH")

    await cached.fetch_quotes([query])
    cached.invalidate(asset_id)
    await cached.fetch_quotes([query])

    assert inner.call_count == 2


@pytest.mark.asyncio
async def test_cache_full_clear() -> None:
    inner = CountingProvider()
    cached = CachedMarketDataProvider(inner, ttl_seconds=60)

    ids = [uuid4() for _ in range(3)]
    queries = [AssetMarketQuery(asset_id=aid, symbol=f"T{i}") for i, aid in enumerate(ids)]

    await cached.fetch_quotes(queries)
    assert cached.cache_size == 3

    cached.invalidate()
    assert cached.cache_size == 0

    await cached.fetch_quotes(queries)
    assert inner.call_count == 2


@pytest.mark.asyncio
async def test_cache_partial_hit() -> None:
    inner = CountingProvider()
    cached = CachedMarketDataProvider(inner, ttl_seconds=60)

    id1, id2 = uuid4(), uuid4()
    q1 = AssetMarketQuery(asset_id=id1, symbol="BTC")
    q2 = AssetMarketQuery(asset_id=id2, symbol="ETH")

    await cached.fetch_quotes([q1])
    await cached.fetch_quotes([q1, q2])

    assert inner.call_count == 2
    assert cached.cache_size == 2


@pytest.mark.asyncio
async def test_cache_expired_ttl() -> None:
    inner = CountingProvider()
    cached = CachedMarketDataProvider(inner, ttl_seconds=0)

    asset_id = uuid4()
    query = AssetMarketQuery(asset_id=asset_id, symbol="BTC")

    await cached.fetch_quotes([query])
    await cached.fetch_quotes([query])

    assert inner.call_count == 2
