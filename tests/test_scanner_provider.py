from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app.config import Settings
from app.providers.binance_futures import BinanceFuturesProvider
from tests.scanner_fixtures import candles

BASE = "https://fapi.binance.com"


def raw_bar(b):
    return [
        int(b.open_time.timestamp() * 1000),
        str(b.open),
        str(b.high),
        str(b.low),
        str(b.close),
        str(b.volume),
        int(b.close_time.timestamp() * 1000),
    ]


@respx.mock
async def test_public_provider_parsing_cache_closed_and_incremental(monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, 10, tzinfo=UTC)
    bars = candles(now=now)
    settings = Settings(scanner_requests_per_second=10)
    async with httpx.AsyncClient(base_url=BASE) as client:
        provider = BinanceFuturesProvider(settings, client)

        async def get(path, params=None):
            return (await client.get(path, params=params)).json()

        monkeypatch.setattr(provider, "_get", get)
        respx.get(BASE + "/fapi/v1/time").respond(
            json={"serverTime": int((now + timedelta(days=1)).timestamp() * 1000)}
        )
        contracts = respx.get(BASE + "/fapi/v1/exchangeInfo").respond(
            json={
                "symbols": [
                    {
                        "symbol": "AAAUSDT",
                        "baseAsset": "AAA",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                    },
                    {},
                ]
            }
        )
        tickers = respx.get(BASE + "/fapi/v1/ticker/24hr").respond(
            json=[
                {
                    "symbol": "AAAUSDT",
                    "lastPrice": "123.456789123456",
                    "quoteVolume": "12345678",
                    "closeTime": int(now.timestamp() * 1000),
                },
                {},
            ]
        )
        assert len(await provider.contracts()) == 1
        await provider.contracts()
        assert contracts.call_count == 1
        assert (await provider.tickers())["AAAUSDT"].price == Decimal(
            "123.456789123456"
        )
        await provider.tickers()
        assert tickers.call_count == 1
        forming = bars[-1].model_copy(
            update={
                "open_time": bars[-1].open_time + timedelta(minutes=15),
                "close_time": bars[-1].close_time + timedelta(minutes=15),
            }
        )
        route = respx.get(BASE + "/fapi/v1/klines").respond(
            json=[raw_bar(b) for b in bars + [forming]]
        )
        result = await provider.candles("AAAUSDT", "15m", now)
        assert result == bars
        assert (
            await provider.candles("AAAUSDT", "15m", now + timedelta(minutes=5)) == bars
        )
        assert route.call_count == 1
        route.respond(json=[raw_bar(bars[-1]), raw_bar(forming)])
        updated = await provider.candles("AAAUSDT", "15m", now + timedelta(minutes=15))
        assert updated[-1] == forming
        assert "startTime" in route.calls[-1].request.url.params
        await provider.aclose()
        assert not client.is_closed


@respx.mock
async def test_derivative_partial_failure_is_unavailable():
    now = datetime.now(UTC)
    provider = BinanceFuturesProvider(
        Settings(scanner_http_retries=0, scanner_requests_per_second=10)
    )
    funding = respx.get(BASE + "/fapi/v1/premiumIndex").respond(
        json=[
            {
                "symbol": s,
                "lastFundingRate": ".0001",
                "time": int(now.timestamp() * 1000),
            }
            for s in ("BTCUSDT", "ETHUSDT")
        ]
    )
    respx.get(BASE + "/fapi/v1/openInterest").respond(503)
    respx.get(BASE + "/futures/data/openInterestHist").respond(
        json=[{"timestamp": int(now.timestamp() * 1000), "sumOpenInterest": "1234"}]
    )
    try:
        data = await provider.derivatives("BTCUSDT", now)
        assert data.funding_rate == Decimal(".0001")
        assert data.open_interest is None and data.errors
        assert data.history[0].contracts == 1234
        other = await provider.derivatives("ETHUSDT", now)
        assert other.funding_rate == Decimal(".0001")
        # Funding for the whole universe comes from one batched request.
        assert funding.call_count == 1
        assert "symbol" not in funding.calls[0].request.url.params
        assert (await provider.derivatives("MISSINGUSDT", now)).funding_rate is None
    finally:
        await provider.aclose()


@respx.mock
async def test_transient_retry_and_permanent_failure(monkeypatch):
    monkeypatch.setattr("app.providers.binance_futures.asyncio.sleep", AsyncMock())
    provider = BinanceFuturesProvider(Settings(scanner_http_retries=1))
    route = respx.get(BASE + "/fapi/v1/exchangeInfo").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"symbols": []}),
        ]
    )
    try:
        assert await provider.contracts() == []
        assert route.call_count == 2
        route.respond(400)
        provider._cache.clear()
        with pytest.raises(httpx.HTTPStatusError):
            await provider.contracts()
        assert route.call_count == 3
    finally:
        await provider.aclose()


@respx.mock
async def test_revised_closed_candle_fails_once_then_rebuilds(monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, 10, tzinfo=UTC)
    bars = candles(now=now)
    provider = BinanceFuturesProvider(Settings(scanner_requests_per_second=10))
    respx.get(BASE + "/fapi/v1/time").respond(
        json={"serverTime": int((now + timedelta(days=1)).timestamp() * 1000)}
    )
    route = respx.get(BASE + "/fapi/v1/klines").respond(json=[raw_bar(b) for b in bars])
    try:
        await provider.candles("AAAUSDT", "15m", now)
        revised = bars[-1].model_copy(update={"volume": bars[-1].volume + 1})
        route.respond(json=[raw_bar(revised)])
        later = now + timedelta(minutes=15)
        with pytest.raises(ValueError, match="revised"):
            await provider.candles("AAAUSDT", "15m", later)
        route.respond(json=[raw_bar(b) for b in bars[:-1] + [revised]])
        rebuilt = await provider.candles("AAAUSDT", "15m", later)
        assert rebuilt[-1] == revised
        assert "startTime" not in route.calls[-1].request.url.params
    finally:
        await provider.aclose()


def test_request_weights_and_expired_cache_pruning():
    from app.providers.binance_futures import request_weight

    assert request_weight("/fapi/v1/ticker/24hr", None) == 40
    assert request_weight("/fapi/v1/premiumIndex", None) == 10
    assert request_weight("/fapi/v1/premiumIndex", {"symbol": "X"}) == 1
    assert request_weight("/fapi/v1/klines", {"limit": 300}) == 2
    assert request_weight("/fapi/v1/klines", {"limit": 499}) == 2
    assert request_weight("/fapi/v1/klines", {"limit": 500}) == 5


async def test_expired_time_bucketed_cache_entries_are_pruned(monkeypatch):
    provider = BinanceFuturesProvider(Settings())
    monkeypatch.setattr(provider, "_get", AsyncMock(return_value=[]))
    try:
        for end in range(50):
            await provider._cached("/futures/data/openInterestHist", 0, {"e": end})
        assert len(provider._cache) == 1 and len(provider._cache_locks) == 1
    finally:
        await provider.aclose()


async def test_long_exchange_ban_fails_fast_instead_of_holding_scan():
    from app.providers.request_control import RateLimitBlockedError, RequestBudget

    clock = [0.0]
    budget = RequestBudget(10, 1200, clock=lambda: clock[0], sleep=AsyncMock())
    budget.defer(3600)
    with pytest.raises(RateLimitBlockedError):
        await budget.acquire(1)
    clock[0] = 3601
    await budget.acquire(1)
