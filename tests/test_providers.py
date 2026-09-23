from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import respx

from app.providers.binance import BinanceMarketProvider
from app.providers.interfaces import AssetMarketQuery


@pytest.mark.asyncio
@respx.mock
async def test_binance_provider_skips_invalid_symbols_after_batch_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "symbols" in request.url.params:
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

        symbol = request.url.params.get("symbol")
        if symbol == "FETUSDT":
            return httpx.Response(
                200,
                json={
                    "symbol": "FETUSDT",
                    "lastPrice": "0.123",
                    "quoteVolume": "10000",
                    "priceChangePercent": "-4.2",
                },
            )
        if symbol == "CCUSDT":
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
        raise AssertionError(f"Unexpected symbol query: {symbol}")

    respx.get("https://api.binance.com/api/v3/ticker/24hr").mock(side_effect=handler)

    provider = BinanceMarketProvider(
        http_client=httpx.AsyncClient(base_url="https://api.binance.com"),
    )
    queries = [
        AssetMarketQuery(
            asset_id=uuid4(),
            symbol="FET",
            name="Artificial Superintelligence Alliance",
            coingecko_id="artificial-superintelligence-alliance",
            binance_symbol="FETUSDT",
        ),
        AssetMarketQuery(
            asset_id=uuid4(),
            symbol="CC",
            name="Canton",
            coingecko_id="canton",
            binance_symbol="CCUSDT",
        ),
    ]

    try:
        quotes = await provider.fetch_quotes(queries)
    finally:
        await provider.aclose()

    assert list(quotes.keys()) == [queries[0].asset_id]
    quote = quotes[queries[0].asset_id]
    assert quote.price_usd == Decimal("0.123")
    assert quote.change_24h_pct == Decimal("-4.2")
    assert quote.source_timestamp is not None
    assert quote.source_timestamp <= datetime.now(timezone.utc)
