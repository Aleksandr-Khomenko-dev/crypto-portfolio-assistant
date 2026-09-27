"""Outcome tracking follows each setup's stored exchange, not CPDA_SCANNER_PROVIDER."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.scanner_models import MarketSetup, SetupOutcome
from app.providers.binance_futures import BinanceFuturesProvider
from app.providers.bingx_futures import BingXFuturesProvider
from app.providers.futures_factory import ProviderRegistry
from app.services.outcome_service import OutcomeService
from app.services.scanner_service import ScannerRuntime, ScannerService
from tests.scanner_fixtures import FixtureFutures
from tests.test_research_service import after_ready
from tests.test_scanner_service import save_ready


class Exchange(FixtureFutures):
    """A scanning exchange that also serves prepared candles for tracked setups."""

    def __init__(self, exchange, outcome_bars):
        super().__init__()
        self.exchange = exchange
        self.outcome_bars = outcome_bars
        self.requested = []

    async def candles(self, symbol, timeframe, now):
        self.requested.append(symbol)
        if symbol in self.outcome_bars:
            return self.outcome_bars[symbol]
        return await super().candles(symbol, timeframe, now)

    async def range_candles(self, *args):
        return []


def tracked_setup(session, symbol, exchange, ranges):
    """A READY setup's outcome on `exchange`, plus that exchange's later candles."""
    _, _, result, _ = save_ready(session)
    row = session.scalar(  # the setup just created (earlier ones were renamed)
        select(MarketSetup).where(
            MarketSetup.symbol == "AAAUSDT", MarketSetup.direction == "LONG"
        )
    )
    row.symbol, row.exchange = symbol, exchange
    session.commit()
    outcome = session.scalar(
        select(SetupOutcome).where(SetupOutcome.market_setup_id == row.id)
    )
    return outcome, after_ready(result, *ranges)


@pytest.fixture()
def two_exchanges(session):
    bingx_outcome, bingx_bars = tracked_setup(
        session, "SOLUSDT", "BINGX", [(0.3, -0.2), (0.4, -0.1), (0.2, -0.3)]
    )
    binance_outcome, binance_bars = tracked_setup(
        session, "XRPUSDT", "BINANCE", [(0.1, -0.4), (0.2, -0.3), (0.1, -0.2)]
    )
    bingx = Exchange("BINGX", {"SOLUSDT": bingx_bars})
    binance = Exchange("BINANCE", {"XRPUSDT": binance_bars})
    registry = ProviderRegistry(
        Settings(), {"BINGX": bingx, "BINANCE": binance}, factory=None
    )
    return bingx_outcome, binance_outcome, bingx, binance, registry, bingx_bars


async def test_switching_bingx_binance_bingx_keeps_every_outcome_on_its_exchange(
    session, two_exchanges
):
    bingx_outcome, binance_outcome, bingx, binance, registry, bars = two_exchanges
    for phase, scanning in enumerate((bingx, binance, bingx), start=1):
        # Each phase is a scanner restarted with a different CPDA_SCANNER_PROVIDER.
        runtime = ScannerRuntime(scanning, Settings(), registry=registry)
        run = await ScannerService(session, runtime).run(
            bars[phase - 1].close_time + timedelta(seconds=30)
        )
        assert run.telemetry["exchange"] == scanning.exchange
        session.refresh(bingx_outcome)
        session.refresh(binance_outcome)
        assert bingx_outcome.bars_processed == phase  # not interrupted by the switch
        assert binance_outcome.bars_processed == phase
    # No cross-exchange candle mixing: each tracked symbol only ever hit its exchange.
    assert "XRPUSDT" not in bingx.requested and "SOLUSDT" not in binance.requested
    assert bingx.requested.count("SOLUSDT") == 3
    assert binance.requested.count("XRPUSDT") == 3
    # Each outcome's excursions come from its own exchange's candles.
    assert bingx_outcome.max_favorable_excursion_r == pytest.approx(0.4)
    assert binance_outcome.max_favorable_excursion_r == pytest.approx(0.2)
    assert binance_outcome.max_adverse_excursion_r == pytest.approx(-0.4)


async def test_bingx_outcome_after_scanner_switched_to_binance(session, two_exchanges):
    bingx_outcome, _, bingx, binance, registry, bars = two_exchanges
    runtime = ScannerRuntime(binance, Settings(), registry=registry)
    await ScannerService(session, runtime).run(
        bars[0].close_time + timedelta(seconds=30)
    )
    session.refresh(bingx_outcome)
    assert bingx_outcome.bars_processed == 1 and bingx.requested == ["SOLUSDT"]


async def test_binance_outcome_after_scanner_switched_to_bingx(session, two_exchanges):
    _, binance_outcome, _, binance, registry, bars = two_exchanges
    stats = await OutcomeService(session, registry, Settings()).process(
        bars[0].close_time + timedelta(seconds=30)
    )
    session.refresh(binance_outcome)
    assert binance_outcome.bars_processed == 1 and binance.requested == ["XRPUSDT"]
    assert stats["outcomes_updated"] == 2


async def test_scanner_bars_only_reused_for_the_same_exchange(session, two_exchanges):
    _, binance_outcome, _, binance, registry, bars = two_exchanges
    # The scanner fetched XRPUSDT on BingX this cycle; the Binance XRPUSDT setup must
    # still use Binance candles, never those BingX bars.
    await OutcomeService(session, registry, Settings()).process(
        bars[0].close_time + timedelta(seconds=30),
        {("BINGX", "XRPUSDT", "15m"): bars},
    )
    session.refresh(binance_outcome)
    assert binance.requested == ["XRPUSDT"]
    assert binance_outcome.max_favorable_excursion_r == pytest.approx(0.1)


async def test_unavailable_exchange_is_reported_and_left_tracking(
    session, two_exchanges
):
    _, _, bingx, _, _, bars = two_exchanges
    only_bingx = ProviderRegistry(Settings(), {"BINGX": bingx}, factory=None)
    stats = await OutcomeService(session, only_bingx, Settings()).process(
        bars[0].close_time + timedelta(seconds=30)
    )
    assert stats["outcomes_provider_unavailable"] == 1
    tracking = session.scalars(
        select(SetupOutcome).join(MarketSetup).where(MarketSetup.exchange == "BINANCE")
    ).all()
    assert [o.outcome_status for o in tracking] == ["TRACKING"]


async def test_registry_builds_each_exchange_lazily_and_keeps_scanner_provider():
    scanning = BingXFuturesProvider(Settings())
    runtime = ScannerRuntime(scanning, Settings())
    try:
        assert runtime.registry.get("BINGX") is scanning
        binance = runtime.registry.get("BINANCE")
        assert isinstance(binance, BinanceFuturesProvider)
        assert runtime.registry.get("BINANCE") is binance  # one long-lived instance
        with pytest.raises(LookupError):
            runtime.registry.get("KRAKEN")
    finally:
        await runtime.aclose()
