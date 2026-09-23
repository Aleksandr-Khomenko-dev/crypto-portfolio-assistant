"""Score-band aggregation of completed setup outcomes (pure)."""

import pytest

from app.research.calibration import OutcomeRecord, bands, calibrate
from app.research.outcomes import (
    AMBIGUOUS,
    AMBIGUOUS_SAME_BAR,
    COMPLETED_2R,
    DATA_GAP,
    EXPIRED,
    INVALIDATED,
    PENDING,
    UNRESOLVED,
)
from app.research.outcomes import (
    INVALIDATION_FIRST as STOP,
)
from app.research.outcomes import (
    TARGET_FIRST as HIT,
)

EDGES = (60, 70, 80, 90)


def record(score, status, first, mfe=1.0, mae=-0.5, symbol="ARBUSDT", direction="LONG"):
    return OutcomeRecord(
        symbol=symbol,
        direction=direction,
        timeframe="15m",
        market_regime="BULLISH",
        structure_regime="TREND",
        score=score,
        status=status,
        first=dict(zip(("0_5r", "1r", "1_5r", "2r"), first)),
        mfe_r=mfe,
        mae_r=mae,
    )


def test_default_bands():
    assert bands(EDGES) == [(0, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
    assert bands((0, 50)) == [(0, 49), (50, 100)]


def test_band_rates_exclude_ambiguous_and_expired_but_report_them():
    records = [
        record(85, COMPLETED_2R, (HIT, HIT, HIT, HIT), mfe=2.1, mae=-0.2),
        record(82, INVALIDATED, (HIT, HIT, STOP, STOP), mfe=1.2, mae=-1.0),
        record(88, INVALIDATED, (STOP, STOP, STOP, STOP), mfe=0.1, mae=-1.0),
        record(80, AMBIGUOUS_SAME_BAR, (HIT, AMBIGUOUS, STOP, STOP), mfe=1.0, mae=-1.0),
        record(
            89, EXPIRED, (HIT, UNRESOLVED, UNRESOLVED, UNRESOLVED), mfe=0.7, mae=-0.3
        ),
        record(84, DATA_GAP, (PENDING,) * 4),
        record(81, "TRACKING", (PENDING,) * 4),
    ]
    [group] = calibrate(records, EDGES, min_samples=30)
    band = next(b for b in group.bands if b.band == "80–89")
    assert band.sample_count == 5  # DATA_GAP and still-tracking are not samples
    assert band.data_gap_count == 1
    assert band.ambiguous_count == 1 and band.expired_count == 1
    rates = {t.target: t for t in band.targets}
    assert (rates["+0.5R"].target_first, rates["+0.5R"].resolved) == (4, 5)
    assert rates["+0.5R"].observed_rate == pytest.approx(0.8)
    one_r = rates["+1R"]
    assert (one_r.target_first, one_r.invalidation_first, one_r.resolved) == (2, 1, 3)
    assert (one_r.ambiguous, one_r.unresolved) == (1, 1)
    assert one_r.observed_rate == pytest.approx(2 / 3)
    assert rates["+2R"].observed_rate == pytest.approx(1 / 4)
    assert band.avg_mfe_r == pytest.approx(1.02)
    assert band.median_mfe_r == pytest.approx(1.0)
    assert band.avg_mae_r == pytest.approx(-0.7)
    assert band.median_mae_r == pytest.approx(-1.0)
    assert band.low_sample


def test_sample_size_threshold_and_empty_bands():
    records = [record(95, INVALIDATED, (HIT, STOP, STOP, STOP)) for _ in range(3)]
    [group] = calibrate(records, EDGES, min_samples=3)
    by_band = {b.band: b for b in group.bands}
    assert "0–59" not in by_band  # shown only when observed
    assert by_band["90–100"].sample_count == 3 and not by_band["90–100"].low_sample
    assert by_band["60–69"].sample_count == 0 and by_band["60–69"].low_sample
    assert all(t.observed_rate is None for t in by_band["60–69"].targets)
    assert by_band["60–69"].avg_mfe_r is None
    assert group.sample_count == 3


def test_below_threshold_scores_get_their_own_band():
    [group] = calibrate([record(40, EXPIRED, (UNRESOLVED,) * 4)], EDGES)
    assert group.bands[0].band == "0–59" and group.bands[0].sample_count == 1


def test_grouping_by_direction_and_asset_class():
    records = [
        record(85, INVALIDATED, (HIT, STOP, STOP, STOP), symbol="BTCUSDT"),
        record(85, INVALIDATED, (STOP,) * 4, symbol="ARBUSDT"),
        record(75, INVALIDATED, (STOP,) * 4, symbol="ARBUSDT", direction="SHORT"),
    ]
    groups = calibrate(records, EDGES, ["direction", "asset_class"])
    keys = [g.key for g in groups]
    assert keys == [
        {"direction": "LONG", "asset_class": "ALT"},
        {"direction": "LONG", "asset_class": "MAJOR"},
        {"direction": "SHORT", "asset_class": "ALT"},
    ]
    assert [g.sample_count for g in groups] == [1, 1, 1]
    with pytest.raises(ValueError, match="Unsupported"):
        calibrate(records, EDGES, ["score_component"])
