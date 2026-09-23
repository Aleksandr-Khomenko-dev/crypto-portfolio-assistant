from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analytics.analysis import analyze_frame
from app.analytics.market_context import UnavailableContextProvider, market_context
from app.config import Settings
from app.scanner.domain import (
    Derivatives,
    Direction,
    MarketContext,
    StructureBreak,
    Zone,
)
from app.scanner.risk import risk_plan
from app.scanner.scoring import BLOCK_CAPS, score_setup, signal_state
from app.scanner.universe import filter_universe
from tests.scanner_fixtures import FixtureFutures, candles


def aligned_frames(direction=Direction.LONG):
    now = datetime.now(UTC)
    frames = {
        tf: analyze_frame(candles(tf, now), tf, now, Settings())
        for tf in ("15m", "1h", "4h")
    }
    sign = 1 if direction == Direction.LONG else -1
    for f in frames.values():
        price = f.candle.close
        f.technical.alignment = "BULLISH" if sign == 1 else "BEARISH"
        f.technical.rvol = 2
        f.technical.adx = 35
        f.technical.rsi = 60 if sign == 1 else 40
        f.technical.plus_di, f.technical.minus_di = (30, 10) if sign == 1 else (10, 30)
        f.technical.extension_atr = 0.5
        f.technical.momentum_pct = sign * 2
        f.macro.trend = f.micro.trend = f.technical.alignment
        f.micro.retest = direction
        f.micro.breaks = [
            StructureBreak(
                index=238,
                timestamp=f.candle.close_time - timedelta(minutes=15),
                level=price - Decimal(sign),
                direction=direction,
                kind="BOS",
            )
        ]

        def zone(offset):
            return Zone(
                lower=price + Decimal(str(offset - 0.2)),
                upper=price + Decimal(str(offset + 0.2)),
                created_at=f.candle.close_time,
                age_bars=10,
                interactions=2,
                distance_atr=abs(offset) / f.technical.atr,
            )

        f.supports = [zone(-1 if sign == 1 else -5)]
        f.resistances = [zone(5 if sign == 1 else 1)]
    return frames


@pytest.mark.parametrize("direction", list(Direction))
def test_scores_symmetric_bounded_and_ready(direction):
    settings = Settings()
    frames = aligned_frames(direction)
    context = MarketContext(
        state="BULLISH" if direction == Direction.LONG else "BEARISH"
    )
    data = Derivatives(
        funding_rate=Decimal(".0001"),
        funding_state="NEUTRAL",
        interpretation="NEW_LONG_PARTICIPATION_POSSIBLE"
        if direction == Direction.LONG
        else "SHORT_BUILD_POSSIBLE",
    )
    matching = score_setup("AAAUSDT", direction, frames, data, context, settings)
    opposite = score_setup(
        "AAAUSDT",
        Direction.SHORT if direction == Direction.LONG else Direction.LONG,
        frames,
        data,
        context,
        settings,
    )
    assert matching.score > opposite.score
    assert matching.readiness == "READY" and matching.risk.valid
    for setup in (matching, opposite):
        assert 0 <= setup.score <= 100
        assert sum(setup.blocks.values()) == setup.score
        assert all(0 <= value <= BLOCK_CAPS[key] for key, value in setup.blocks.items())


@pytest.mark.parametrize(
    "score,state",
    [
        (59, "IGNORE"),
        (60, "WATCH"),
        (69, "WATCH"),
        (70, "SETUP_FORMING"),
        (80, "HIGH_CONFLUENCE"),
        (90, "EXTREME_CONFLUENCE"),
        (100, "EXTREME_CONFLUENCE"),
    ],
)
def test_configurable_states(score, state):
    assert signal_state(score, Settings()) == state
    assert signal_state(59, Settings(scanner_watch_score=50)) == "WATCH"


def test_risk_no_room_extension_and_missing_stop():
    frames = aligned_frames()
    frames["15m"].technical.extension_atr = 100
    assert risk_plan(Direction.LONG, frames, Settings()).reason == "OVEREXTENDED"
    for frame in frames.values():
        frame.resistances[0].lower = frame.candle.close
    assert risk_plan(Direction.LONG, frames, Settings()).reason == "NO_ROOM"
    for frame in frames.values():
        frame.supports = []
    assert risk_plan(Direction.LONG, frames, Settings()).reason == "NO_STRUCTURAL_STOP"


def test_high_score_is_not_automatically_ready_and_risk_off_penalty():
    frames = aligned_frames()
    frames["15m"].micro.retest = None
    setup = score_setup(
        "ALTUSDT",
        Direction.LONG,
        frames,
        Derivatives(),
        MarketContext(state="BULLISH"),
        Settings(),
    )
    assert setup.readiness == "WAIT_RETEST"
    bearish = score_setup(
        "ALTUSDT",
        Direction.LONG,
        frames,
        Derivatives(),
        MarketContext(state="RISK_OFF"),
        Settings(),
    )
    assert bearish.score < setup.score
    frames["15m"].micro.retest = Direction.LONG
    frames["15m"].technical.rvol = None
    assert (
        score_setup(
            "ALTUSDT",
            Direction.LONG,
            frames,
            Derivatives(),
            MarketContext(),
            Settings(),
        ).readiness
        == "WAIT_VOLUME"
    )


async def test_universe_filters_and_context_availability():
    provider = FixtureFutures()
    contracts, tickers = await provider.contracts(), await provider.tickers()
    settings = Settings(scanner_blacklist="AAAUSDT")
    assert filter_universe(contracts, tickers, settings) == ["BBBUSDT"]
    assert filter_universe(
        contracts, tickers, Settings(scanner_whitelist="AAAUSDT", scanner_max_markets=1)
    ) == ["AAAUSDT"]
    assert (
        filter_universe(
            contracts, tickers, Settings(scanner_min_quote_volume_usd=999999999)
        )
        == []
    )
    for update in (
        {"status": "BREAK"},
        {"quote_asset": "USDC"},
        {"contract_type": "CURRENT_QUARTER"},
        {"underlying_subtypes": ["LEVERAGED"]},
        {"underlying_type": "INDEX"},
    ):
        assert (
            filter_universe(
                [contracts[0].model_copy(update=update)], tickers, Settings()
            )
            == []
        )
    assert market_context({}, Settings()).state == "unavailable"
    assert (
        await UnavailableContextProvider().context("BTCUSDT")
    ).state == "unavailable"
    bullish = aligned_frames()
    assert (
        market_context({"BTCUSDT": bullish, "ETHUSDT": bullish}, Settings()).state
        == "BULLISH"
    )
    bearish = aligned_frames(Direction.SHORT)
    bearish["1h"].macro.breaks = bearish["1h"].micro.breaks
    assert (
        market_context({"BTCUSDT": bearish, "ETHUSDT": bearish}, Settings()).state
        == "RISK_OFF"
    )
