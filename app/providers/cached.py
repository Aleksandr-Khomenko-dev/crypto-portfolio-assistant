from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote

logger = logging.getLogger(__name__)

# (quote, expires_at_monotonic)
_CacheEntry = tuple[MarketQuote, float]


class CachedMarketDataProvider(AbstractMarketDataProvider):
    """In-memory TTL cache wrapper around any AbstractMarketDataProvider.

    A single instance is meant to live for the lifetime of the app process
    so cache entries are shared across all requests.
    """

    provider_name = "cached"

    def __init__(self, inner: AbstractMarketDataProvider, ttl_seconds: int = 300) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._cache: dict[UUID, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        quotes = await self.fetch_quotes([query])
        return quotes.get(query.asset_id)

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        now = time.monotonic()

        hits: dict[UUID, MarketQuote] = {}
        misses: list[AssetMarketQuery] = []

        for query in queries:
            entry = self._cache.get(query.asset_id)
            if entry is not None and entry[1] > now:
                hits[query.asset_id] = entry[0]
            else:
                misses.append(query)

        if not misses:
            logger.debug("Quote cache: all %d hits", len(hits))
            return hits

        logger.debug("Quote cache: %d hits, %d misses — fetching from provider", len(hits), len(misses))
        fresh = await self._inner.fetch_quotes(misses)

        expires_at = time.monotonic() + self._ttl
        async with self._lock:
            for asset_id, quote in fresh.items():
                self._cache[asset_id] = (quote, expires_at)

        return {**hits, **fresh}

    def invalidate(self, asset_id: UUID | None = None) -> None:
        if asset_id is None:
            self._cache.clear()
            logger.debug("Quote cache cleared")
        else:
            self._cache.pop(asset_id, None)
            logger.debug("Quote cache invalidated for %s", asset_id)

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    async def aclose(self) -> None:
        await self._inner.aclose()
