from datetime import timedelta
from decimal import Decimal

import pytest

from app.analytics.analysis import analyze_frame
from app.analytics.derivatives import normalize_derivatives
from app.analytics.levels import fair_value_gaps, zones
from app.analytics.structure import pivots, structure, successful_retest
from app.analytics.technical import adx, atr, ema, rsi, rvol, technical
from app.config import Settings
from app.scanner.domain import Derivatives, Direction, OIPoint, StructureBreak
from tests.scanner_fixtures import candles, price_bars


def test_ema_sma_seed_and_recurrence():
    assert ema([1, 2, 3, 4, 5], 3) == [2, 3, 4]
    with pytest.raises(ValueError):
        ema([1], 3)


def test_atr_rsi_adx_flat_and_directional():
    bars = price_bars([100] * 220)
    assert atr(bars) == pytest.approx(0.4)
    assert rsi([100] * 30) == 50
    assert rsi(list(range(30))) == 100
    assert adx(bars) == (0, 0, 0)
    rising = price_bars(list(range(1, 221)))
    strength, plus, minus = adx(rising)
    assert strength == pytest.approx(100)
    assert plus > minus == 0
    t = technical(rising)
    assert t.alignment == "BULLISH"
    assert all(t.price_above_ema.values())
    assert all(v > 0 for v in t.ema_slope_atr.values())


def test_rvol_excludes_signal_bar_from_baseline():
    bars = price_bars([100] * 21)
    bars = [b.model_copy(update={"volume": Decimal(100)}) for b in bars]
    bars[-1] = bars[-1].model_copy(update={"volume": Decimal(250)})
    assert rvol(bars) == 2.5
    assert rvol([b.model_copy(update={"volume": Decimal(0)}) for b in bars]) is None


def test_bos_choch_and_no_wick_confirmation():
    bars = price_bars([10, 12, 10, 13, 11, 14, 12, 15, 8])
    result = structure(bars, 1, Decimal(".1"))
    assert any(e.kind == "BOS" and e.direction == Direction.LONG for e in result.breaks)
    assert result.breaks[-1].kind == "CHoCH"
    assert result.breaks[-1].direction == Direction.SHORT
    bars = price_bars([10, 12, 10, 13, 11, 14, 12, 15, 13, 14])
    wick = bars[-1].model_copy(
        update={
            "open": Decimal(14),
            "close": Decimal(14),
            "high": Decimal(20),
            "low": Decimal(13),
        }
    )
    closed = structure(bars[:-1] + [wick], 1, Decimal(".1"))
    assert not any(e.index == 9 and e.direction == "LONG" for e in closed.breaks)
    assert any(
        e.index == 9 and e.direction == "LONG"
        for e in structure(bars[:-1] + [wick], 1, Decimal(".1"), False).breaks
    )


def test_pivots_do_not_use_unconfirmed_right_edge():
    bars = price_bars([10, 12, 10, 13])
    assert all(p.index != 3 for p in pivots(bars, 1))
    assert pivots(bars, 1)[0].confirmed_index == 2


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_liquidity_sweep_and_retest(direction):
    prices = [10, 12, 10, 13, 11, 14, 12]
    bars = price_bars(prices)
    if direction == Direction.LONG:
        final = bars[-1].model_copy(
            update={
                "low": Decimal(9),
                "high": Decimal(12),
                "open": Decimal(11),
                "close": Decimal(12),
            }
        )
        level = Decimal(11)
    else:
        final = bars[-1].model_copy(
            update={
                "high": Decimal(15),
                "low": Decimal(12),
                "open": Decimal(13),
                "close": Decimal(12),
            }
        )
        level = Decimal(13)
    bars.append(
        final.model_copy(
            update={
                "open_time": bars[-1].open_time + timedelta(minutes=15),
                "close_time": bars[-1].close_time + timedelta(minutes=15),
            }
        )
    )
    assert structure(bars, 1, Decimal(".1")).sweep == direction
    event = StructureBreak(
        index=len(bars) - 2,
        timestamp=bars[-2].close_time,
        level=level,
        direction=direction,
        kind="BOS",
    )
    assert successful_retest(bars, event, Decimal(".1"))
    assert not successful_retest(bars[:-1], event, Decimal(".1"))


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_fvg_detection_and_mitigation_states(direction):
    prices = [100] * 20 + ([102, 104] if direction == Direction.LONG else [98, 96])
    bars = price_bars(prices)
    gaps = fair_value_gaps(bars, "15m", 0.15)
    gap = gaps[-1]
    assert gap.direction == direction and gap.status == "FRESH"
    assert gap.midpoint == (gap.lower + gap.upper) / 2
    for depth, status in [
        (gap.midpoint, "CE_TOUCHED"),
        (gap.lower if direction == Direction.LONG else gap.upper, "FILLED"),
    ]:
        last = bars[-1]
        update = {"low": depth} if direction == Direction.LONG else {"high": depth}
        mitigation = last.model_copy(update=update)
        assert (
            fair_value_gaps(bars + [mitigation], "15m", 0.15)[len(gaps) - 1].status
            == status
        )
    invalid = bars[-1].model_copy(
        update={
            "close": gap.lower - 1 if direction == Direction.LONG else gap.upper + 1
        }
    )
    assert (
        fair_value_gaps(bars + [invalid], "15m", 0.15)[len(gaps) - 1].status
        == "INVALIDATED"
    )
    assert not fair_value_gaps(bars, "15m", 100)


def test_zones_are_ranges_with_interaction_and_distance_data():
    bars = price_bars([10, 12, 10, 13, 11, 14, 12, 13])
    supports, resistances = zones(bars, pivots(bars, 1), 1, 0.3)
    assert supports and resistances
    assert all(
        z.upper > z.lower and z.interactions >= 1 and z.distance_atr >= 0
        for z in supports + resistances
    )


@pytest.mark.parametrize(
    "oi_end,price_end,interpretation",
    [
        (110, 110, "NEW_LONG_PARTICIPATION_POSSIBLE"),
        (90, 110, "SHORT_COVERING_POSSIBLE"),
        (110, 90, "SHORT_BUILD_POSSIBLE"),
        (90, 90, "DELEVERAGING_POSSIBLE"),
    ],
)
def test_derivatives_use_aligned_intervals(oi_end, price_end, interpretation):
    bars = price_bars([100, price_end])
    history = [
        OIPoint(timestamp=b.open_time + timedelta(minutes=15), contracts=n)
        for b, n in zip(bars, [100, oi_end])
    ]
    normalized = normalize_derivatives(
        Derivatives(funding_rate=Decimal(".002"), history=history),
        bars,
        Decimal(".001"),
    )
    assert normalized.interpretation == interpretation
    assert normalized.oi_change_pct == pytest.approx(oi_end - 100)
    assert normalized.funding_state == "CROWDED_LONG"
    assert (
        normalize_derivatives(Derivatives(), bars, Decimal(".001")).interpretation
        == "unavailable"
    )


def test_forming_candle_cannot_change_confirmed_analysis():
    bars = candles()
    now = bars[-1].close_time + timedelta(seconds=11)
    forming = bars[-1].model_copy(
        update={
            "open_time": bars[-1].open_time + timedelta(minutes=15),
            "close_time": bars[-1].close_time + timedelta(minutes=15),
            "close": Decimal(10000),
        }
    )
    before = analyze_frame(bars, "15m", now, Settings())
    assert before == analyze_frame(bars + [forming], "15m", now, Settings())
    with pytest.raises(ValueError, match="Stale"):
        analyze_frame(bars, "15m", now + timedelta(hours=1), Settings())
    with pytest.raises(ValueError, match="Gapped"):
        analyze_frame(bars[:50] + bars[51:], "15m", now, Settings())


def test_stale_derivatives_receive_no_credit():
    bars = price_bars([100, 110])
    now = bars[-1].close_time + timedelta(seconds=1)
    data = Derivatives(
        funding_rate=Decimal(".0001"),
        open_interest=123,
        funding_timestamp=now - timedelta(hours=2),
        oi_timestamp=now - timedelta(hours=2),
    )
    normalized = normalize_derivatives(data, bars, Decimal(".001"), now)
    assert normalized.funding_rate is None and normalized.open_interest is None
    assert normalized.funding_state == "unavailable"
    assert len(normalized.errors) == 2


def test_checkpoint_older_than_history_rebuilds_instead_of_failing():
    settings = Settings()
    now = candles(count=1)[0].close_time + timedelta(seconds=10)
    old_now = now - timedelta(days=10)
    old = analyze_frame(candles(now=old_now, count=260), "15m", old_now, settings)
    fresh = analyze_frame(candles(now=now, count=260), "15m", now, settings)
    rebuilt = analyze_frame(candles(now=now, count=260), "15m", now, settings, old)
    assert rebuilt.macro == fresh.macro and rebuilt.fvgs == fresh.fvgs


def test_fvg_checkpoint_matches_cold_rebuild_and_stays_bounded():
    bars = candles(count=260, slope=0.4)
    first = fair_value_gaps(bars[:-40], "15m", 0.0)
    rolled = bars[40:]
    incremental = fair_value_gaps(rolled, "15m", 0.0, first, bars[:-40][-1].close_time)
    assert all(g.created_at >= rolled[13].close_time for g in incremental)
    assert len(incremental) <= len(fair_value_gaps(rolled, "15m", 0.0)) + len(first)
