"""Gold-only mode: TradFi opt-in, real-market hours, gold-sized fast floors."""

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.db.session import get_session_factory
from app.fast.watcher import FastMarketWatcher, config_from
from app.scanner.domain import Contract, Ticker
from app.scanner.market_hours import follows_market_hours, metals_market_open
from app.scanner.universe import display_symbol, filter_universe
from app.scheduler.jobs import build_scheduler
from app.services.scanner_service import ScannerRuntime, ScannerService
from app.telegram.fast import tradingview_url
from tests.scanner_fixtures import FixtureFutures

GOLD = "NCCOGOLD2USDUSDT"  # BingX GOLD(XAU)-USDT
SATURDAY = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
WEDNESDAY = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def contracts():
    return [
        Contract(
            symbol=GOLD,
            base_asset="NCCOGOLD2USD",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
            underlying_type="TRADFI",
        ),
        Contract(  # Binance gold: contractType TRADIFI_PERPETUAL, COMMODITY
            symbol="XAUUSDT",
            base_asset="XAU",
            quote_asset="USDT",
            contract_type="TRADIFI_PERPETUAL",
            status="TRADING",
            underlying_type="COMMODITY",
            underlying_subtypes=["TradFi"],
        ),
        Contract(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
        ),
    ]


def tickers():
    now = datetime.now(UTC)
    return {
        c.symbol: Ticker(symbol=c.symbol, price=1, quote_volume=1e9, timestamp=now)
        for c in contracts()
    }


@pytest.mark.parametrize(
    ("at", "is_open"),
    [
        (datetime(2026, 11, 7, 17, 0, tzinfo=UTC), False),  # Saturday
        (datetime(2026, 11, 8, 22, 59, tzinfo=UTC), False),  # Sun 17:59 New York (EST)
        (datetime(2026, 11, 8, 23, 0, tzinfo=UTC), True),  # Sun 18:00 New York reopen
        (datetime(2026, 11, 6, 21, 59, tzinfo=UTC), True),  # Fri 16:59 New York
        (datetime(2026, 11, 6, 22, 0, tzinfo=UTC), False),  # Fri 17:00 New York close
        (datetime(2026, 11, 4, 22, 30, tzinfo=UTC), False),  # Wed daily break
        (datetime(2026, 11, 4, 23, 0, tzinfo=UTC), True),  # Wed 18:00 New York
        (datetime(2026, 7, 5, 22, 0, tzinfo=UTC), True),  # Sun 18:00 EDT (summer)
        (datetime(2026, 7, 3, 21, 0, tzinfo=UTC), False),  # Fri 17:00 EDT (summer)
    ],
)
def test_metals_market_hours_follow_new_york_time(at, is_open):
    assert metals_market_open(at) is is_open


def test_only_non_crypto_underlyings_follow_market_hours():
    assert follows_market_hours("TRADFI") and follows_market_hours("COMMODITY")
    assert not follows_market_hours("COIN")


def test_gold_is_scanned_only_when_explicitly_whitelisted():
    assert filter_universe(contracts(), tickers(), Settings()) == ["BTCUSDT"]
    assert filter_universe(
        contracts(), tickers(), Settings(scanner_whitelist=GOLD)
    ) == [GOLD]
    assert filter_universe(
        contracts(), tickers(), Settings(scanner_whitelist="XAUUSDT")
    ) == ["XAUUSDT"]
    # The whitelist opt-in never admits a non-USDT or non-trading contract.
    for update in ({"status": "BREAK"}, {"quote_asset": "USDC"}):
        gold = contracts()[0].model_copy(update=update)
        assert (
            filter_universe([gold], tickers(), Settings(scanner_whitelist=GOLD)) == []
        )


def test_gold_display_name_and_reference_chart():
    assert display_symbol(GOLD) == "GOLD (XAU/USD)"
    assert display_symbol("NCFXAUD2USDUSDT") == "AUD/USD"
    assert display_symbol("BTCUSDT") == "BTCUSDT"
    assert tradingview_url(GOLD) == (
        "https://www.tradingview.com/chart/?symbol=OANDA:XAUUSD"
    )


def test_fast_floors_scale_to_gold_volatility():
    crypto, gold = config_from(Settings()), config_from(Settings(fast_floor_scale=0.25))
    assert crypto.min_pct["1m"] == 1.0
    assert gold.min_pct["1m"] == pytest.approx(0.25)
    assert gold.min_pct["15s"] == pytest.approx(0.25)
    assert gold.extreme_pct["5m"] == pytest.approx(1.5)


class GoldBingX:
    async def contracts(self):
        return contracts()

    async def tickers(self):
        return tickers()


async def test_fast_watcher_follows_whitelist_and_market_hours(sqlite_database_url):
    watcher = FastMarketWatcher(
        Settings(scanner_whitelist=GOLD),
        get_session_factory(sqlite_database_url),
        GoldBingX(),
        None,
        None,
    )
    assert await watcher.universe() == [GOLD]
    assert watcher.exclusions["BTCUSDT"] == "NOT_WHITELISTED"
    assert watcher.market_hours_symbols == {GOLD}
    assert watcher._market_closed(GOLD, SATURDAY.timestamp())
    assert not watcher._market_closed(GOLD, WEDNESDAY.timestamp())
    watcher.settings.scanner_market_hours_gate = False
    assert not watcher._market_closed(GOLD, SATURDAY.timestamp())


def test_portfolio_jobs_can_be_switched_off():
    def job_ids(**overrides):
        return {job.id for job in build_scheduler(Settings(**overrides)).get_jobs()}

    assert {"portfolio-monitor", "morning-digest"} <= job_ids()
    remaining = job_ids(portfolio_monitoring_enabled=False, scanner_enabled=False)
    assert remaining == {"price-snapshot-cleanup"}


class TradFiFixture(FixtureFutures):
    async def contracts(self):
        rows = await super().contracts()
        return [
            row.model_copy(update={"underlying_type": "TRADFI"})
            if row.symbol == "AAAUSDT"
            else row
            for row in rows
        ]


@pytest.mark.parametrize(("now", "expected"), [(SATURDAY, 1), (WEDNESDAY, 2)])
async def test_scanner_skips_tradfi_while_its_market_is_closed(session, now, expected):
    settings = Settings(
        scanner_whitelist="AAAUSDT,BBBUSDT", scanner_use_btc_context=False
    )
    runtime = ScannerRuntime(TradFiFixture(), settings)
    run = await ScannerService(session, runtime).run(now=now)
    assert run.universe_size == expected
    await runtime.aclose()


class RecordingContexts:
    def __init__(self):
        self.majors_calls, self.built = 0, []

    async def majors(self, now):
        self.majors_calls += 1
        return {"BTCUSDT": {}}

    async def build(self, symbol, now, majors, spread):
        self.built.append((symbol, majors))
        raise RuntimeError("context isolated in this test")


async def test_gold_early_context_never_uses_btc_eth(sqlite_database_url):
    watcher = FastMarketWatcher(
        Settings(scanner_whitelist=GOLD),
        get_session_factory(sqlite_database_url),
        GoldBingX(),
        None,
        None,
    )
    await watcher.universe()
    watcher.contexts = RecordingContexts()
    await watcher.refresh_contexts([GOLD])
    assert watcher.contexts.majors_calls == 0  # BTC/ETH not even downloaded
    assert watcher.contexts.built == [(GOLD, {})]
