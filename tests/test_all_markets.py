"""Crypto plus named TradFi markets (metals, FX, indices, energy) and bounded history."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.db.scanner_models import MarketSetup, ScannerRun, ScannerSnapshot
from app.db.session import get_session_factory
from app.fast.watcher import FastMarketWatcher
from app.macro.calendar import EconomicEvent
from app.scanner.domain import Contract, Direction, MarketContext, Ticker
from app.scanner.retention import prune, prune_batch
from app.scanner.universe import display_symbol, filter_universe
from app.services.scanner_service import ScannerRuntime, ScannerService, symbol_context
from app.telegram.fast import tradingview_url
from app.telegram.i18n import catalog
from app.telegram.scanner import context_block
from tests.scanner_fixtures import FixtureFutures

GOLD, SILVER, EURUSD = "NCCOGOLD2USDUSDT", "NCCOXAG2USDUSDT", "NCFXEUR2USDUSDT"
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)  # Wednesday, markets open


def contract(symbol: str, tradfi: bool = False) -> Contract:
    return Contract(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        underlying_type="TRADFI" if tradfi else "COIN",
    )


CONTRACTS = [
    contract("BTCUSDT"),
    contract("ETHUSDT"),
    contract("SOLUSDT"),
    contract(GOLD, tradfi=True),
    contract(SILVER, tradfi=True),
    contract(EURUSD, tradfi=True),
]
VOLUMES = {
    "BTCUSDT": 9e9,
    "ETHUSDT": 5e9,
    "SOLUSDT": 1e9,
    GOLD: 2e8,
    SILVER: 8e6,  # below the crypto volume floor
    EURUSD: 6e5,  # BingX FX: small volume, tight quotes
}


def tickers() -> dict[str, Ticker]:
    return {
        s: Ticker(symbol=s, price=1, quote_volume=v, timestamp=NOW, bid=1, ask=1.0001)
        for s, v in VOLUMES.items()
    }


def test_named_tradfi_markets_are_added_to_the_crypto_top_n():
    tradfi = f"{GOLD},{SILVER},{EURUSD}"
    assert filter_universe(CONTRACTS, tickers(), Settings()) == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    ]
    # Outside the crypto cap and the volume floor; ordered by volume after crypto.
    assert filter_universe(
        CONTRACTS, tickers(), Settings(scanner_tradfi_symbols=tradfi, scanner_top_n=2)
    ) == ["BTCUSDT", "ETHUSDT", GOLD, SILVER, EURUSD]
    # Only the named ones: other TradFi markets still need the explicit opt-in.
    assert filter_universe(
        CONTRACTS, tickers(), Settings(scanner_tradfi_symbols=SILVER.lower())
    ) == ["BTCUSDT", "ETHUSDT", "SOLUSDT", SILVER]
    # A whitelist still restricts everything; the blacklist still applies.
    assert filter_universe(
        CONTRACTS,
        tickers(),
        Settings(scanner_whitelist=f"{GOLD},BTCUSDT", scanner_tradfi_symbols=tradfi),
    ) == ["BTCUSDT", GOLD]
    assert filter_universe(
        CONTRACTS,
        tickers(),
        Settings(scanner_tradfi_symbols=tradfi, scanner_blacklist=EURUSD),
    ) == ["BTCUSDT", "ETHUSDT", "SOLUSDT", GOLD, SILVER]


def test_a_named_market_still_needs_a_trading_contract_and_a_ticker():
    halted = [c.model_copy(update={"status": "BREAK"}) for c in CONTRACTS]
    settings = Settings(scanner_tradfi_symbols=SILVER)
    assert SILVER not in filter_universe(halted, tickers(), settings)
    no_ticker = {s: t for s, t in tickers().items() if s != SILVER}
    assert SILVER not in filter_universe(CONTRACTS, no_ticker, settings)


@pytest.mark.parametrize(
    ("symbol", "name", "chart"),
    [
        (SILVER, "SILVER (XAG/USD)", "OANDA:XAGUSD"),
        ("NCCOXPT2USDUSDT", "PLATINUM (XPT/USD)", "OANDA:XPTUSD"),
        ("NCCOPALLADIUM2USDUSDT", "PALLADIUM (XPD/USD)", "OANDA:XPDUSD"),
        ("NCCO724COPPER2USDUSDT", "COPPER", "OANDA:XCUUSD"),
        ("NCCO1OILWTI2USDUSDT", "OIL WTI", "OANDA:WTICOUSD"),
        ("NCCO1OILBRENT2USDUSDT", "OIL BRENT", "OANDA:BCOUSD"),
        ("NCSINASDAQ1002USDUSDT", "NASDAQ 100", "OANDA:NAS100USD"),
        ("NCSISP5002USDUSDT", "S&P 500", "OANDA:SPX500USD"),
        (EURUSD, "EUR/USD", "OANDA:EURUSD"),
        ("NCFXGBP2JPYUSDT", "GBP/JPY", "OANDA:GBPJPY"),
        ("SOLUSDT", "SOLUSDT", "BINGX:SOLUSDT.P"),
    ],
)
def test_names_and_free_reference_charts(symbol, name, chart):
    assert display_symbol(symbol) == name
    assert (
        tradingview_url(symbol) == f"https://www.tradingview.com/chart/?symbol={chart}"
    )


def test_odd_fx_names_fall_back_to_the_exchange_chart():
    # USDBRL quoted in USD is not a plain currency pair on OANDA.
    assert tradingview_url("NCFXUSDBRL2USDUSDT").endswith("BINGX:NCFXUSDBRL2USDUSDT.P")


def test_btc_context_never_applies_to_a_tradfi_market():
    btc = MarketContext(state="RISK_OFF", explanation="BTC risk-off")
    tradfi = {GOLD, SILVER, EURUSD}
    assert symbol_context("SOLUSDT", btc, None, [], tradfi) is btc
    for symbol in (SILVER, EURUSD):
        context = symbol_context(symbol, btc, None, [], tradfi)
        assert context.kind == "NONE"
        assert context.state == "unavailable"  # no context points, no RISK_OFF cap
        assert context.event is None
    assert symbol_context(GOLD, btc, None, [], tradfi).kind == "GOLD_MACRO"


def test_the_calendar_pause_applies_to_every_tradfi_market():
    event = EconomicEvent(
        title="Non-Farm Employment Change", at=NOW + timedelta(minutes=10)
    )
    btc = MarketContext(state="BULLISH")
    paused = symbol_context(EURUSD, btc, None, [event], {EURUSD})
    assert paused.event == "Non-Farm Employment Change"
    assert paused.event_at == event.at
    assert symbol_context("SOLUSDT", btc, None, [event], {EURUSD}).event is None


def test_signal_shows_no_btc_block_for_metals_and_fx():
    t = catalog("ru")
    none = MarketContext(kind="NONE")
    assert context_block(none, Direction.LONG, t) == []
    paused = none.model_copy(update={"event": "CPI m/m", "event_at": NOW})
    assert context_block(paused, Direction.LONG, t) == [
        "📅 Пауза сигналов: CPI m/m · 11.11 15:00 UTC",
        "",
    ]
    crypto = context_block(MarketContext(state="BULLISH"), Direction.LONG, t)
    assert crypto[0] == t["section.context"] and crypto[-1] == ""


class TradFiExchange(FixtureFutures):
    """AAAUSDT is a TradFi market here; BTC/ETH candles come from the fixture."""

    async def contracts(self):
        return [
            row.model_copy(update={"underlying_type": "TRADFI"})
            if row.symbol == "AAAUSDT"
            else row
            for row in await super().contracts()
        ]


async def test_scan_keeps_btc_context_for_crypto_only(session):
    settings = Settings(scanner_tradfi_symbols="AAAUSDT")
    runtime = ScannerRuntime(TradFiExchange(), settings)
    try:
        run = await ScannerService(session, runtime).run(now=NOW)
    finally:
        await runtime.aclose()
    assert run.universe_size == 2
    kinds = {
        row.symbol: row.data["context"]["kind"]
        for row in session.query(ScannerSnapshot).all()
    }
    assert kinds == {"AAAUSDT": "NONE", "BBBUSDT": "CRYPTO"}


class Exchange:
    async def contracts(self):
        return CONTRACTS

    async def tickers(self):
        return tickers()


async def test_fast_watcher_follows_named_tradfi_markets(sqlite_database_url):
    settings = Settings(scanner_tradfi_symbols=f"{SILVER},{EURUSD}")
    watcher = FastMarketWatcher(
        settings, get_session_factory(sqlite_database_url), Exchange(), None, None
    )
    universe = await watcher.universe()
    assert set(universe) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", SILVER, EURUSD}
    assert watcher.exclusions[GOLD] == "NOT_USDT_COIN_PERPETUAL"  # not named
    assert watcher.market_hours_symbols == {SILVER, EURUSD}
    # Crypto keeps crypto-sized floors; TradFi markets get the TradFi scale.
    assert watcher.config.min_pct["1m"] == 1.0
    assert watcher.tradfi_config.min_pct["1m"] == pytest.approx(0.25)


# --- bounded history ------------------------------------------------------------------


def add_run(session, at: datetime) -> ScannerRun:
    run = ScannerRun(started_at=at, config_json={"context_frames": {}}, errors={})
    session.add(run)
    session.flush()
    return run


def add_snapshot(session, run: ScannerRun, at: datetime) -> ScannerSnapshot:
    snapshot = ScannerSnapshot(
        run_id=run.id,
        symbol="BTCUSDT",
        exchange="BINGX",
        created_at=at,
        candle_closed_at=at,
        long_score=50,
        short_score=40,
        data={"frames": "x" * 100},
    )
    session.add(snapshot)
    session.flush()
    return snapshot


async def test_retention_deletes_old_history_but_keeps_what_setups_need(
    session, sqlite_database_url
):
    old, recent = NOW - timedelta(days=5), NOW - timedelta(hours=2)
    old_run, kept_run = add_run(session, old), add_run(session, old)
    stale = [add_snapshot(session, old_run, old) for _ in range(5)]
    referenced = add_snapshot(session, kept_run, old)
    fresh_run = add_run(session, recent)
    fresh = add_snapshot(session, fresh_run, recent)
    empty_old_run = add_run(session, old - timedelta(hours=1))
    session.add(
        MarketSetup(
            snapshot_id=referenced.id,
            symbol="BTCUSDT",
            exchange="BINGX",
            direction="LONG",
            score=81,
            state="HIGH_CONFLUENCE",
            readiness="READY",
            price=Decimal(1),
            created_at=old,
            updated_at=old,
            expires_at=old + timedelta(hours=4),
            data={},
        )
    )
    session.commit()
    stale_ids = {s.id for s in stale}
    gone_runs = {old_run.id, empty_old_run.id}

    sessions = get_session_factory(sqlite_database_url)
    assert prune_batch(sessions, NOW - timedelta(days=3), batch=2) == 2  # in batches
    deleted = await prune(sessions, NOW, 3, pause=0)
    assert deleted == 3 + 2  # the other stale snapshots, then the emptied runs

    session.expire_all()
    left = {row.id for row in session.query(ScannerSnapshot).all()}
    assert left == {referenced.id, fresh.id}
    assert not stale_ids & left
    runs = {row.id for row in session.query(ScannerRun).all()}
    assert runs == {kept_run.id, fresh_run.id}  # old_run and empty_old_run deleted
    assert not gone_runs & runs
    assert await prune(sessions, NOW, 3, pause=0) == 0


async def test_retention_keeps_the_latest_run_even_when_old(
    session, sqlite_database_url
):
    old = NOW - timedelta(days=10)
    only = add_run(session, old)
    session.commit()
    sessions = get_session_factory(sqlite_database_url)
    assert await prune(sessions, NOW, 3, pause=0) == 0
    session.expire_all()
    assert session.get(ScannerRun, only.id) is not None
