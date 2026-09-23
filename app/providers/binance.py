from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

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


class BinanceMarketProvider(AbstractMarketDataProvider):
    provider_name = "binance"

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            base_url=self.settings.binance_base_url.rstrip("/"),
            timeout=self.settings.http_timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        quotes = await self.fetch_quotes([query])
        return quotes.get(query.asset_id)

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        if not queries:
            return {}

        symbol_map: dict[str, AssetMarketQuery] = {}
        for query in queries:
            market_symbol = (query.binance_symbol or f"{query.symbol.upper()}USDT").upper()
            symbol_map[market_symbol] = query

        try:
            response = await self.client.get(
                "/api/v3/ticker/24hr",
                params={"symbols": json.dumps(list(symbol_map.keys()))},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                payload = [payload]
            return self._parse_quotes_payload(payload, symbol_map)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            return await self._fetch_quotes_one_by_one(symbol_map)

    async def _fetch_quotes_one_by_one(
        self,
        symbol_map: dict[str, AssetMarketQuery],
    ) -> dict[UUID, MarketQuote]:
        quotes: dict[UUID, MarketQuote] = {}

        for market_symbol, query in symbol_map.items():
            try:
                response = await self.client.get(
                    "/api/v3/ticker/24hr",
                    params={"symbol": market_symbol},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400:
                    continue
                raise

            record = response.json()
            if not isinstance(record, dict):
                continue

            quote = self._build_quote(query, record)
            if quote is not None:
                quotes[query.asset_id] = quote

        return quotes

    def _parse_quotes_payload(
        self,
        payload: list[dict[str, Any]],
        symbol_map: dict[str, AssetMarketQuery],
    ) -> dict[UUID, MarketQuote]:
        quotes: dict[UUID, MarketQuote] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            market_symbol = str(item.get("symbol", "")).upper()
            query = symbol_map.get(market_symbol)
            if query is None:
                continue

            quote = self._build_quote(query, item)
            if quote is not None:
                quotes[query.asset_id] = quote
        return quotes

    def _build_quote(
        self,
        query: AssetMarketQuery,
        item: dict[str, Any],
    ) -> MarketQuote | None:
        price = _to_decimal(item.get("lastPrice"))
        if price is None:
            return None

        return MarketQuote(
            asset_id=query.asset_id,
            provider=self.provider_name,
            symbol=query.symbol.upper(),
            price_usd=price,
            market_cap_usd=None,
            volume_24h_usd=_to_decimal(item.get("quoteVolume")),
            change_1h_pct=None,
            change_24h_pct=_to_decimal(item.get("priceChangePercent")),
            source_timestamp=datetime.now(timezone.utc),
            raw=item,
        )
