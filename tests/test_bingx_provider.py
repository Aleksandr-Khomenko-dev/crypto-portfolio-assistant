"""BingX adapter tests. Every HTTP call is mocked with payload shapes captured from the
live public API on 2026-09-23 (objects newest-first, error envelopes on HTTP 200)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.analytics.derivatives import normalize_derivatives
from app.config import Settings
from app.db.scanner_models import MarketSetup, ScannerSnapshot, SetupOutcome
from app.providers.bingx_futures import (
    BingXAPIError,
    BingXFuturesProvider,
    bingx_to_internal_symbol,
    internal_to_bingx_symbol,
    parse_contract,
)
from app.providers.futures_factory import create_futures_provider
from app.providers.request_control import RequestBudget
from app.scanner.domain import INTERVAL_SECONDS, Derivatives
from app.scanner.universe import filter_universe
from app.services.outcome_service import OutcomeService
from app.services.scanner_service import ScannerRuntime, ScannerService
from tests.scanner_fixtures import candles

BASE = "https://open-api.bingx.com"
NOW = datetime(2026, 9, 23, 12, 7, 30, tzinfo=UTC)


def ok(data, remain=499):
    return httpx.Response(
        200,
        json={"code": 0, "msg": "", "data": data},
        headers={
            "x-ratelimit-requests-remain": str(remain),
            "x-ratelimit-requests-expire": "10000",
        },
    )


def contract(symbol, status=1, api_open="true", currency="USDT"):
    return {
        "contractId": "1",
        "symbol": symbol,
        "currency": currency,
        "asset": symbol.split("-")[0],
        "status": status,
        "apiStateOpen": api_open,
        "apiStateClose": "true",
        "offTime": 0,
        "displayName": symbol,
    }


def ticker(symbol, quote_volume, price="1.5"):
    return {
        "symbol": symbol,
        "lastPrice": price,
        "volume": "1000",
        "quoteVolume": str(quote_volume),
        "openTime": 1790109811000,
        "closeTime": int(NOW.timestamp() * 1000),
    }


def kline_rows(bars, forming=True):
    """BingX v3 shape: newest-first objects, `time` = open time, plus forming candle."""
    rows = [
        {
            "open": str(b.open),
            "close": str(b.close),
            "high": str(b.high),
            "low": str(b.low),
            "volume": str(b.volume),
            "time": int(b.open_time.timestamp() * 1000),
        }
        for b in bars
    ]
    if forming:
        last = bars[-1]
        step = int((last.close_time - last.open_time).total_seconds() + 0.001) * 1000
        rows.append({**rows[-1], "time": rows[-1]["time"] + step, "high": "999999"})
    return list(reversed(rows))


def settings(**overrides):
    return Settings(
        **{"scanner_requests_per_second": 10, "scanner_http_retries": 1, **overrides}
    )


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.providers.bingx_futures.asyncio.sleep", AsyncMock())


# --- symbols ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("internal", "bingx"),
    [
        ("BTCUSDT", "BTC-USDT"),
        ("ARBUSDT", "ARB-USDT"),
        ("1000PEPEUSDT", "1000PEPE-USDT"),
    ],
)
def test_symbol_normalization_round_trip(internal, bingx):
    assert internal_to_bingx_symbol(internal) == bingx
    assert bingx_to_internal_symbol(bingx) == internal


@pytest.mark.parametrize("bad", ["BTC", "USDT", "btcusdt", "BTC-USDT"])
def test_internal_symbol_validation(bad):
    with pytest.raises(ValueError):
        internal_to_bingx_symbol(bad)


@pytest.mark.parametrize("bad", ["BTCUSDT", "BTC-USDC", "-USDT", "A-B-USDT"])
def test_bingx_symbol_validation(bad):
    with pytest.raises(ValueError):
        bingx_to_internal_symbol(bad)


# --- contracts, tickers, universe -------------------------------------------------


def test_contract_parsing_and_classification():
    assert parse_contract(contract("BTC-USDT")).model_dump() == {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "contract_type": "PERPETUAL",
        "status": "TRADING",
        "underlying_type": "COIN",
        "underlying_subtypes": [],
    }
    assert parse_contract(contract("STG-USDT", status=25)).status == "NOT_TRADING"
    assert (
        parse_contract(contract("MAGMA-USDT", api_open="false")).status == "NOT_TRADING"
    )
    assert parse_contract(contract("NCSKTSLA2USD-USDT")).underlying_type == "TRADFI"
    assert parse_contract(contract("NCFXEUR2USD-USDT")).underlying_type == "TRADFI"
    usdc = parse_contract(contract("BTC-USDC", currency="USDC"))
    assert (usdc.symbol, usdc.quote_asset) == ("BTCUSDC", "USDC")


@respx.mock
async def test_universe_from_bingx_contracts_and_quote_volume():
    respx.get(BASE + "/openApi/swap/v2/quote/contracts").mock(
        return_value=ok(
            [
                contract("BTC-USDT"),
                contract("ETH-USDT"),
                contract("ARB-USDT"),
                contract("DEAD-USDT", status=25),
                contract("MAGMA-USDT", api_open="false"),
                contract("NCSKTSLA2USD-USDT"),
                contract("BTC-USDC", currency="USDC"),
                contract("THIN-USDT"),
                {"symbol": None},
            ]
        )
    )
    respx.get(BASE + "/openApi/swap/v2/quote/ticker").mock(
        return_value=ok(
            [
                ticker("BTC-USDT", 1_063_252_817.81, "84372.5"),
                ticker("ETH-USDT", 831_297_944.98),
                ticker("ARB-USDT", 28_791_248.51),
                ticker("DEAD-USDT", 99_000_000),
                ticker("MAGMA-USDT", 99_000_000),
                ticker("NCSKTSLA2USD-USDT", 99_000_000),
                ticker("BTC-USDC", 99_000_000),
                ticker("THIN-USDT", 1_000),
                {"symbol": "BROKEN-USDT"},
            ]
        )
    )
    provider = BingXFuturesProvider(settings())
    try:
        contracts, tickers = await provider.contracts(), await provider.tickers()
        assert tickers["BTCUSDT"].quote_volume == Decimal("1063252817.81")
        assert tickers["BTCUSDT"].price == Decimal("84372.5")
        assert "BTCUSDC" not in tickers and "BROKENUSDT" not in tickers
        universe = filter_universe(contracts, tickers, settings())
        assert universe == ["BTCUSDT", "ETHUSDT", "ARBUSDT"]
        assert filter_universe(contracts, tickers, settings(scanner_top_n=2)) == [
            "BTCUSDT",
            "ETHUSDT",
        ]
        assert filter_universe(
            contracts, tickers, settings(scanner_blacklist="ethusdt")
        ) == ["BTCUSDT", "ARBUSDT"]
        assert filter_universe(
            contracts, tickers, settings(scanner_whitelist="ARBUSDT")
        ) == ["ARBUSDT"]
    finally:
        await provider.aclose()


# --- klines ------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", ["15m", "1h", "4h"])
@respx.mock
async def test_kline_mapping_and_closed_candle_filtering(timeframe):
    bars = candles(timeframe, NOW)
    respx.get(BASE + "/openApi/swap/v2/server/time").mock(
        return_value=ok({"serverTime": int(NOW.timestamp() * 1000)})
    )
    route = respx.get(BASE + "/openApi/swap/v3/quote/klines").mock(
        return_value=ok(kline_rows(bars))
    )
    provider = BingXFuturesProvider(settings())
    try:
        result = await provider.candles("ARBUSDT", timeframe, NOW)
        assert result == bars  # ascending, forming candle removed, fields mapped
        step = timedelta(seconds=INTERVAL_SECONDS[timeframe])
        assert all(
            b.close_time == b.open_time + step - timedelta(milliseconds=1)
            for b in result
        )
        assert result[-1].close_time < NOW
        params = route.calls[0].request.url.params
        assert params["symbol"] == "ARB-USDT" and params["interval"] == timeframe
        assert int(params["endTime"]) == int(result[-1].close_time.timestamp() * 1000)
        # Same closed boundary later in the interval: served from cache.
        await provider.candles("ARBUSDT", timeframe, NOW + timedelta(seconds=30))
        assert route.call_count == 1
    finally:
        await provider.aclose()


@respx.mock
async def test_incremental_fetch_and_forming_candle_never_admitted():
    bars = candles("15m", NOW)
    later = NOW + timedelta(minutes=15)
    step = timedelta(minutes=15)
    newer = [
        bars[-1],
        bars[-1].model_copy(
            update={
                "open_time": bars[-1].open_time + step,
                "close_time": bars[-1].close_time + step,
            }
        ),
    ]
    respx.get(BASE + "/openApi/swap/v2/server/time").mock(
        return_value=ok({"serverTime": int(later.timestamp() * 1000)})
    )
    route = respx.get(BASE + "/openApi/swap/v3/quote/klines").mock(
        return_value=ok(kline_rows(bars))
    )
    provider = BingXFuturesProvider(settings())
    try:
        await provider.candles("BTCUSDT", "15m", NOW)
        route.mock(return_value=ok(kline_rows(newer)))
        updated = await provider.candles("BTCUSDT", "15m", later)
        assert updated[-1] == newer[-1] and updated[-2] == bars[-1]
        assert "startTime" in route.calls[-1].request.url.params
        assert all(b.high != Decimal(999999) for b in updated)
    finally:
        await provider.aclose()


@respx.mock
async def test_range_candles_only_inside_parent():
    parent_open = datetime(2026, 9, 23, 11, 45, tzinfo=UTC)
    parent_close = parent_open + timedelta(minutes=15, milliseconds=-1)
    five = [
        b
        for b in candles("5m", parent_open + timedelta(minutes=20), count=6)
        if b.open_time >= parent_open - timedelta(minutes=5)
    ]
    respx.get(BASE + "/openApi/swap/v3/quote/klines").mock(
        return_value=ok(kline_rows(five, forming=False))
    )
    provider = BingXFuturesProvider(settings())
    try:
        kids = await provider.range_candles("ARBUSDT", "5m", parent_open, parent_close)
        assert [k.open_time for k in kids] == [
            parent_open + timedelta(minutes=5 * i) for i in range(3)
        ]
    finally:
        await provider.aclose()


# --- funding and open interest ---------------------------------------------------


def premium(symbol, rate="0.00007500", hours=8, mark="84371.9"):
    return {
        "symbol": symbol,
        "markPrice": mark,
        "indexPrice": mark,
        "lastFundingRate": rate,
        "nextFundingTime": 1790208000000,
        "fundingIntervalHours": hours,
        "updateTime": 1790179200000,
    }


@respx.mock
async def test_funding_and_open_interest_mapping_and_units():
    funding = respx.get(BASE + "/openApi/swap/v2/quote/premiumIndex").mock(
        return_value=ok([premium("BTC-USDT"), premium("ARB-USDT", "0.0002", 1, "0.2")])
    )
    respx.get(BASE + "/openApi/swap/v2/quote/openInterest").mock(
        side_effect=lambda request: ok(
            {
                "openInterest": "903681521.9"
                if request.url.params["symbol"] == "BTC-USDT"
                else "4430130.93361",
                "symbol": request.url.params["symbol"],
                "time": int(NOW.timestamp() * 1000),
            }
        )
    )
    provider = BingXFuturesProvider(settings())
    try:
        btc = await provider.derivatives("BTCUSDT", NOW)
        arb = await provider.derivatives("ARBUSDT", NOW)
    finally:
        await provider.aclose()
    assert funding.call_count == 1  # one bulk request serves every contract
    assert "symbol" not in funding.calls[0].request.url.params
    assert btc.funding_rate == Decimal("0.00007500")
    assert btc.funding_interval_hours == 8 and arb.funding_interval_hours == 1
    assert btc.next_funding_at == datetime.fromtimestamp(1790208000, UTC)
    assert abs((btc.funding_timestamp - datetime.now(UTC)).total_seconds()) < 60
    # BingX OI is USDT notional; base quantity is derived with the same-cycle mark.
    assert btc.open_interest_notional == Decimal("903681521.9")
    assert btc.open_interest == Decimal("903681521.9") / Decimal("84371.9")
    assert round(btc.open_interest) == 10711
    assert btc.oi_timestamp == NOW and btc.history == [] and not btc.errors


def test_funding_classification_uses_8h_equivalent():
    now = datetime.now(UTC)
    extreme = Decimal("0.001")

    def state(rate, hours):
        data = Derivatives(
            funding_rate=Decimal(rate),
            funding_timestamp=now,
            funding_interval_hours=hours,
        )
        return normalize_derivatives(data, [], extreme, now).funding_state

    assert state("0.0002", 1) == "CROWDED_LONG"  # 0.16% per 8h
    assert state("0.0002", 8) == "NEUTRAL"
    assert state("-0.0005", 4) == "CROWDED_SHORT"  # -0.1% per 8h
    assert state("0.0002", None) == "NEUTRAL"  # unknown interval: as reported


@respx.mock
async def test_derivative_failure_is_isolated_per_metric(no_sleep):
    respx.get(BASE + "/openApi/swap/v2/quote/premiumIndex").mock(
        return_value=ok([premium("BTC-USDT")])
    )
    respx.get(BASE + "/openApi/swap/v2/quote/openInterest").mock(
        return_value=httpx.Response(503)
    )
    provider = BingXFuturesProvider(settings())
    try:
        data = await provider.derivatives("BTCUSDT", NOW)
        missing = await provider.derivatives("NEWUSDT", NOW)
    finally:
        await provider.aclose()
    assert data.funding_rate is not None and data.open_interest_notional is None
    assert data.errors == ["oi: HTTPStatusError"]
    assert missing.funding_rate is None and "funding: KeyError" in missing.errors


# --- errors and rate limits -------------------------------------------------------


@respx.mock
async def test_error_envelope_and_malformed_body_are_not_retried(no_sleep):
    route = respx.get(BASE + "/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(
            200, json={"code": 109425, "msg": "NOPE-USDT not exist", "data": {}}
        )
    )
    provider = BingXFuturesProvider(settings())
    try:
        with pytest.raises(BingXAPIError, match="109425"):
            await provider.range_candles("NOPEUSDT", "5m", NOW, NOW)
        assert route.call_count == 1
        route.mock(return_value=httpx.Response(200, text="<html>"))
        with pytest.raises(BingXAPIError, match="Malformed"):
            await provider.range_candles("NOPEUSDT", "5m", NOW, NOW)
    finally:
        await provider.aclose()


@respx.mock
async def test_429_rate_limit_code_and_5xx_are_retried_with_shared_cooldown(no_sleep):
    route = respx.get(BASE + "/openApi/swap/v2/quote/contracts").mock(
        side_effect=[
            httpx.Response(429, headers={"x-ratelimit-requests-expire": "2000"}),
            httpx.Response(200, json={"code": 100410, "msg": "rate limited"}),
            ok([contract("BTC-USDT")]),
        ]
    )
    provider = BingXFuturesProvider(settings(scanner_http_retries=2))
    deferred = []
    provider._budget.defer = deferred.append
    try:
        assert [c.symbol for c in await provider.contracts()] == ["BTCUSDT"]
    finally:
        await provider.aclose()
    assert route.call_count == 3
    assert deferred == [2.0, 10]
    assert provider.stats["http_429"] == 1 and provider.stats["rate_limit_codes"] == 1


@respx.mock
async def test_low_remaining_quota_pauses_all_callers():
    respx.get(BASE + "/openApi/swap/v2/server/time").mock(
        return_value=ok({"serverTime": 1}, remain=3)
    )
    provider = BingXFuturesProvider(settings())
    deferred = []
    provider._budget.defer = deferred.append
    try:
        await provider._get("/openApi/swap/v2/server/time")
    finally:
        await provider.aclose()
    assert deferred == [10.0]


async def test_request_budget_window_for_bingx():
    clock, slept = [0.0], []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    budget = RequestBudget(
        100, 3, window_seconds=10, clock=lambda: clock[0], sleep=sleep
    )
    for _ in range(4):
        await budget.acquire(1)
    assert slept and clock[0] >= 10  # 4th request waited for the 10 s window


# --- selection, mixing, context, outcomes -----------------------------------------


async def test_provider_factory_selection():
    bingx = create_futures_provider(Settings())
    binance = create_futures_provider(Settings(scanner_provider="binance"))
    try:
        assert bingx.exchange == "BINGX" and binance.exchange == "BINANCE"
    finally:
        await bingx.aclose()
        await binance.aclose()
    with pytest.raises(ValueError):
        Settings(scanner_provider="kraken")


def bingx_routes(now, symbols, failing=()):
    history = {tf: candles(tf, now) for tf in ("15m", "1h", "4h")}
    respx.get(BASE + "/openApi/swap/v2/server/time").mock(
        return_value=ok({"serverTime": int(now.timestamp() * 1000)})
    )
    respx.get(BASE + "/openApi/swap/v2/quote/contracts").mock(
        return_value=ok([contract(s) for s in symbols])
    )
    respx.get(BASE + "/openApi/swap/v2/quote/ticker").mock(
        return_value=ok([ticker(s, 50_000_000, "112") for s in symbols])
    )

    def klines(request):
        if request.url.params["symbol"] in failing:
            return httpx.Response(200, json={"code": 109425, "msg": "not exist"})
        return ok(kline_rows(history[request.url.params["interval"]]))

    requested = respx.get(BASE + "/openApi/swap/v3/quote/klines").mock(
        side_effect=klines
    )
    respx.get(BASE + "/openApi/swap/v2/quote/premiumIndex").mock(
        return_value=ok([premium(s, mark="112") for s in symbols])
    )
    respx.get(BASE + "/openApi/swap/v2/quote/openInterest").mock(
        side_effect=lambda request: ok(
            {
                "openInterest": "1000000",
                "symbol": request.url.params["symbol"],
                "time": int(now.timestamp() * 1000),
            }
        )
    )
    return requested


@respx.mock
async def test_full_scan_on_bingx_context_isolation_and_no_mixing(session, no_sleep):
    now = datetime.now(UTC)
    symbols = ["BTC-USDT", "ETH-USDT", "ARB-USDT", "GONE-USDT"]
    requested = bingx_routes(now, symbols, failing={"GONE-USDT"})
    # A pre-existing Binance episode for the same market must stay untouched.
    legacy = await _legacy_binance_episode(session)
    runtime = ScannerRuntime(BingXFuturesProvider(settings()), settings())
    try:
        run = await ScannerService(session, runtime).run(now)
    finally:
        await runtime.aclose()
    assert run.status == "PARTIAL" and run.analyzed == 3 and run.failed == 1
    assert run.errors == {
        "GONEUSDT": "BingXAPIError"
    }  # one symbol never stops the scan
    assert run.telemetry["exchange"] == "BINGX" and run.telemetry["requests"] > 0
    # BTC/ETH context came from BingX (the only provider in the run).
    context_symbols = {c.request.url.params["symbol"] for c in requested.calls}
    assert {"BTC-USDT", "ETH-USDT"} <= context_symbols
    snapshots = session.scalars(
        select(ScannerSnapshot).where(ScannerSnapshot.exchange == "BINGX")
    ).all()
    assert len(snapshots) == 3
    assert all(s.data["exchange"] == "BINGX" for s in snapshots)
    assert all(s.data["context"]["assets"] for s in snapshots)
    assert all(
        s.data["derivatives"]["open_interest_notional"] == "1000000" for s in snapshots
    )
    session.refresh(legacy)
    assert legacy.exchange == "BINANCE" and legacy.snapshot_id != snapshots[0].id
    assert all(
        s.exchange == "BINGX"
        for s in session.scalars(select(MarketSetup).where(MarketSetup.id != legacy.id))
    )


async def _legacy_binance_episode(session):
    from tests.test_scanner_service import save_ready

    _, _, _, row = save_ready(session)
    row.symbol = "ARBUSDT"
    session.commit()
    assert row.exchange == "BINANCE"
    return row


class _Recorder:
    def __init__(self, exchange, bars):
        self.exchange, self.bars, self.calls = exchange, bars, 0

    async def candles(self, symbol, timeframe, now):
        self.calls += 1
        return self.bars

    async def range_candles(self, *args):
        return []


async def test_outcome_tracking_stays_on_the_setup_exchange(session):
    from tests.test_research_service import after_ready, ready_outcome

    settings_, _, result, row, outcome = ready_outcome(session)
    row.exchange = "BINGX"
    session.commit()
    bars = after_ready(result, (0.6, -0.2))
    binance = _Recorder("BINANCE", bars)
    stats = await OutcomeService(session, binance, settings_).process(
        bars[0].close_time
    )
    session.refresh(outcome)
    assert binance.calls == 0 and outcome.bars_processed == 0 and not stats
    bingx = _Recorder("BINGX", bars)
    await OutcomeService(session, bingx, settings_).process(bars[0].close_time)
    session.refresh(outcome)
    assert bingx.calls == 1 and outcome.bars_processed == 1
    assert session.scalar(select(func.count()).select_from(SetupOutcome)) == 1
