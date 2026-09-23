"""Deterministic setup-outcome engine tests (pure, no I/O)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.research.outcomes import (
    AMBIGUOUS,
    AMBIGUOUS_SAME_BAR,
    COMPLETED_2R,
    DATA_GAP,
    EXPIRED,
    INVALIDATED,
    INVALIDATION_FIRST,
    PENDING,
    TARGET_FIRST,
    TRACKING,
    UNRESOLVED,
    Progress,
    advance,
    build_plan,
)
from app.scanner.domain import Candle, Direction

READY_OPEN = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
READY_CLOSE = READY_OPEN + timedelta(minutes=15, milliseconds=-1)
FAR = datetime(2030, 1, 1, tzinfo=UTC)


def bar(index, high, low, minutes=15, start=None):
    """Closed candle `index` intervals after the READY candle."""
    opened = (start or READY_OPEN) + timedelta(minutes=minutes * index)
    mid = (Decimal(str(high)) + Decimal(str(low))) / 2
    return Candle(
        open_time=opened,
        close_time=opened + timedelta(minutes=minutes, milliseconds=-1),
        open=mid,
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=mid,
        volume=1,
    )


def long_plan():
    return build_plan(Direction.LONG, "15m", Decimal(100), Decimal(90))


def short_plan():
    return build_plan(Direction.SHORT, "15m", Decimal(100), Decimal(110))


def start(plan):
    return Progress(
        last_processed_at=READY_CLOSE,
        max_favorable_price=plan.entry,
        max_adverse_price=plan.entry,
    )


def run(plan, bars, now=FAR, max_bars=96, children=None, progress=None):
    return advance(plan, progress or start(plan), bars, now, max_bars, children)


def test_long_r_targets_from_frozen_risk():
    plan = long_plan()
    assert plan.risk == 10
    assert plan.targets == {
        "0_5r": Decimal(105),
        "1r": Decimal(110),
        "1_5r": Decimal(115),
        "2r": Decimal(120),
    }


def test_short_r_targets_from_frozen_risk():
    plan = short_plan()
    assert plan.risk == 10
    assert plan.targets == {
        "0_5r": Decimal(95),
        "1r": Decimal(90),
        "1_5r": Decimal(85),
        "2r": Decimal(80),
    }


@pytest.mark.parametrize(
    ("direction", "entry", "invalidation"),
    [
        (Direction.LONG, 100, 100),
        (Direction.LONG, 100, 101),
        (Direction.SHORT, 100, 99),
    ],
)
def test_non_positive_risk_is_rejected(direction, entry, invalidation):
    assert build_plan(direction, "15m", Decimal(entry), Decimal(invalidation)) is None


def test_plus_1r_before_minus_1r_long():
    state = run(
        long_plan(),
        [bar(1, 106, 99), bar(2, 111, 104), bar(3, 105, 89), bar(4, 130, 100)],
    ).progress
    assert state.first == {
        "0_5r": TARGET_FIRST,
        "1r": TARGET_FIRST,
        "1_5r": INVALIDATION_FIRST,
        "2r": INVALIDATION_FIRST,
    }
    assert state.outcome_status == INVALIDATED and state.hit_minus_1r
    assert (state.bars_to["0_5r"], state.bars_to["1r"]) == (1, 2)
    assert state.bars_to_invalidation == 3
    assert state.first_event == "TARGET_0_5R"
    assert state.first_event_at == bar(1, 1, 1).close_time
    assert not state.hits["2r"], "bars after a terminal event are never evaluated"
    assert state.bars_processed == 3


def test_minus_1r_before_plus_1r_short():
    state = run(short_plan(), [bar(1, 111, 99), bar(2, 101, 70)]).progress
    assert set(state.first.values()) == {INVALIDATION_FIRST}
    assert state.outcome_status == INVALIDATED
    assert state.first_event == "INVALIDATION" and state.bars_to_invalidation == 1
    assert not any(state.hits.values())


def test_plus_2r_completes_and_multiple_targets_in_one_bar():
    state = run(long_plan(), [bar(1, 104, 99), bar(2, 121, 103)]).progress
    assert set(state.first.values()) == {TARGET_FIRST}
    assert state.outcome_status == COMPLETED_2R
    assert state.bars_to == {"0_5r": 2, "1r": 2, "1_5r": 2, "2r": 2}


def test_same_bar_target_and_invalidation_is_ambiguous_never_guessed():
    step = run(long_plan(), [bar(1, 111, 89)])
    state = step.progress
    assert state.first == {
        "0_5r": AMBIGUOUS,
        "1r": AMBIGUOUS,
        "1_5r": INVALIDATION_FIRST,
        "2r": INVALIDATION_FIRST,
    }
    assert state.outcome_status == AMBIGUOUS_SAME_BAR
    assert state.first_event == AMBIGUOUS_SAME_BAR
    assert step.unresolved_parent == bar(1, 111, 89)


def children_of(parent_index, ranges):
    start = READY_OPEN + timedelta(minutes=15 * parent_index)
    return [bar(i, h, l, minutes=5, start=start) for i, (h, l) in enumerate(ranges)]


def test_lower_timeframe_resolves_target_first():
    parent = bar(1, 111, 89)
    kids = children_of(1, [(106, 99), (111, 100), (101, 89)])
    step = run(long_plan(), [parent], children={parent.open_time: kids})
    state = step.progress
    assert state.first["0_5r"] == state.first["1r"] == TARGET_FIRST
    assert state.first["1_5r"] == state.first["2r"] == INVALIDATION_FIRST
    assert state.outcome_status == INVALIDATED
    assert state.ambiguity_resolution == "LOWER_TIMEFRAME"
    assert (
        state.first_event == "TARGET_0_5R"
        and state.first_event_at == kids[0].close_time
    )
    assert step.unresolved_parent is None


def test_lower_timeframe_resolves_invalidation_first():
    parent = bar(1, 111, 89)
    kids = children_of(1, [(101, 89), (111, 95), (105, 100)])
    state = run(long_plan(), [parent], children={parent.open_time: kids}).progress
    assert set(state.first.values()) == {INVALIDATION_FIRST}
    assert state.outcome_status == INVALIDATED and state.first_event == "INVALIDATION"


def test_lower_timeframe_child_touching_both_stays_ambiguous():
    parent = bar(1, 111, 89)
    kids = children_of(1, [(106, 99), (111, 89), (100, 95)])
    state = run(long_plan(), [parent], children={parent.open_time: kids}).progress
    assert state.first["0_5r"] == TARGET_FIRST
    assert state.first["1r"] == AMBIGUOUS
    assert state.outcome_status == AMBIGUOUS_SAME_BAR


@pytest.mark.parametrize(
    "kids",
    [
        children_of(1, [(106, 99), (111, 89)]),  # incomplete parent
        children_of(1, [(106, 99), (110, 100), (101, 89)]),  # range differs from parent
        children_of(
            1, [(106, 99), (111, 100), (101, 89), (100, 95)]
        ),  # leaks past parent
        children_of(2, [(106, 99), (111, 100), (101, 89)]),  # belongs to another parent
    ],
)
def test_unusable_lower_timeframe_data_never_resolves(kids):
    parent = bar(1, 111, 89)
    state = run(long_plan(), [parent], children={parent.open_time: kids}).progress
    assert state.first["1r"] == AMBIGUOUS
    assert state.outcome_status == AMBIGUOUS_SAME_BAR
    assert state.ambiguity_resolution == "LOWER_TIMEFRAME_UNUSABLE"


def test_mfe_and_mae_long_in_price_and_r():
    state = run(
        long_plan(), [bar(1, 104, 96), bar(2, 104.5, 95), bar(3, 103, 97)]
    ).progress
    assert state.max_favorable_price == Decimal("104.5")
    assert state.max_adverse_price == Decimal(95)
    assert state.max_favorable_excursion_r == pytest.approx(0.45)
    assert state.max_adverse_excursion_r == pytest.approx(-0.5)
    assert state.outcome_status == TRACKING


def test_mfe_and_mae_short_in_price_and_r():
    state = run(short_plan(), [bar(1, 103, 97), bar(2, 106, 98)]).progress
    assert state.max_favorable_price == Decimal(97)
    assert state.max_adverse_price == Decimal(106)
    assert state.max_favorable_excursion_r == pytest.approx(0.3)
    assert state.max_adverse_excursion_r == pytest.approx(-0.6)


def test_gap_through_invalidation_reports_mae_beyond_minus_1r():
    state = run(long_plan(), [bar(1, 88, 85)]).progress
    assert state.outcome_status == INVALIDATED
    assert state.max_adverse_excursion_r == pytest.approx(-1.5)
    assert state.max_favorable_excursion_r == 0


def test_expiry_is_neither_win_nor_loss():
    bars = [bar(1, 106, 99), bar(2, 104, 98), bar(3, 103, 97), bar(4, 125, 80)]
    state = run(long_plan(), bars, max_bars=3).progress
    assert state.outcome_status == EXPIRED
    assert state.expired_at == bars[2].close_time == state.completed_at
    assert state.first["0_5r"] == TARGET_FIRST
    assert state.first["1r"] == state.first["2r"] == UNRESOLVED
    assert not state.hit_minus_1r and state.bars_processed == 3


def test_idempotent_and_incremental_updates():
    plan, bars = long_plan(), [bar(1, 106, 99), bar(2, 104, 98)]
    first = run(plan, bars[:1]).progress
    again = run(plan, bars[:1], progress=first).progress
    assert again == first
    both = run(plan, bars, progress=first).progress
    assert both == run(plan, bars).progress
    assert both.bars_processed == 2


def test_no_candle_used_before_it_is_closed_or_before_ready():
    plan = long_plan()
    ready_bar = bar(0, 130, 50)  # the READY candle itself happened before entry
    earlier = bar(-1, 130, 50)
    forming = bar(1, 130, 50)
    state = run(
        plan,
        [earlier, ready_bar, forming],
        now=forming.close_time - timedelta(seconds=1),
    ).progress
    assert state.bars_processed == 0 and state.first["0_5r"] == PENDING
    state = run(plan, [earlier, ready_bar, forming], now=forming.close_time).progress
    assert state.bars_processed == 1


def test_missing_candles_after_cursor_are_a_data_gap_not_an_outcome():
    state = run(long_plan(), [bar(2, 130, 50)]).progress
    assert state.outcome_status == DATA_GAP
    assert state.bars_processed == 0 and state.first["2r"] == PENDING


def test_terminal_outcome_is_frozen():
    done = run(long_plan(), [bar(1, 105, 89)]).progress
    assert (
        run(long_plan(), [bar(1, 105, 89), bar(2, 200, 100)], progress=done).progress
        == done
    )
