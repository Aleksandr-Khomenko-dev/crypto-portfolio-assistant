from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class CoinGeckoMarketProvider(AbstractMarketDataProvider):
    provider_name = "coingecko"

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            base_url=self.settings.coingecko_base_url.rstrip("/"),
            timeout=self.settings.http_timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.coingecko_demo_api_key:
            headers["x-cg-demo-api-key"] = self.settings.coingecko_demo_api_key
        return headers

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        quotes = await self.fetch_quotes([query])
        return quotes.get(query.asset_id)

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        if not queries:
            return {}

        id_queries = [query for query in queries if query.coingecko_id]
        symbol_queries = [query for query in queries if not query.coingecko_id]
        quotes: dict[UUID, MarketQuote] = {}

        if id_queries:
            quotes.update(await self._fetch_by_ids(id_queries))
        if symbol_queries:
            quotes.update(await self._fetch_by_symbols(symbol_queries))
        return quotes

    async def _fetch_by_ids(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        id_map = {query.coingecko_id: query for query in queries if query.coingecko_id}
        response = await self.client.get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(id_map.keys()),
                "price_change_percentage": "1h,24h",
                "precision": "full",
                "per_page": len(id_map),
                "page": 1,
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        return {
            query.asset_id: self._parse_quote(query, item)
            for item in payload
            if isinstance(item, dict)
            for query in [id_map.get(item.get("id"))]
            if query is not None
        }

    async def _fetch_by_symbols(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        symbol_map = {query.symbol.upper(): query for query in queries}
        response = await self.client.get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "symbols": ",".join(symbol_map.keys()),
                "include_tokens": "top",
                "price_change_percentage": "1h,24h",
                "precision": "full",
                "per_page": max(50, len(symbol_map) * 2),
                "page": 1,
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()

        resolved: dict[UUID, MarketQuote] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            query = symbol_map.get(symbol)
            if query is None or query.asset_id in resolved:
                continue
            resolved[query.asset_id] = self._parse_quote(query, item)
        return resolved

    def _parse_quote(self, query: AssetMarketQuery, record: dict[str, Any]) -> MarketQuote:
        last_updated = record.get("last_updated")
        source_timestamp = None
        if isinstance(last_updated, str):
            try:
                source_timestamp = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            except ValueError:
                source_timestamp = None

        return MarketQuote(
            asset_id=query.asset_id,
            provider=self.provider_name,
            symbol=query.symbol.upper(),
            price_usd=_to_decimal(record.get("current_price")) or Decimal("0"),
            market_cap_usd=_to_decimal(record.get("market_cap")),
            volume_24h_usd=_to_decimal(record.get("total_volume")),
            change_1h_pct=_to_decimal(record.get("price_change_percentage_1h_in_currency")),
            change_24h_pct=(
                _to_decimal(record.get("price_change_percentage_24h_in_currency"))
                or _to_decimal(record.get("price_change_percentage_24h"))
            ),
            source_timestamp=source_timestamp or datetime.now(timezone.utc),
            raw=record,
        )
