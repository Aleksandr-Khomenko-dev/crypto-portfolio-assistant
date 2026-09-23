"""Deterministic setup-outcome engine. Pure functions only: no I/O, no clock.

Definitions (see docs/outcome-tracker.md):

* Tracking starts at the first READY evaluation of a setup episode. The entry
  reference is that READY closed candle's close; only candles that open after it
  are evaluated.
* -1R is the invalidation price frozen at READY. Targets are +0.5R/+1R/+1.5R/+2R
  from the same frozen risk and are never recomputed.
* Each closed candle's HIGH and LOW decide touches. When one candle touches both
  invalidation and an unresolved target, the order is unknown: the target is marked
  AMBIGUOUS unless complete lower-timeframe candles inside that parent resolve it.
* Tracking ends at invalidation, at +2R, or after the configured bar horizon.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.scanner.domain import INTERVAL_SECONDS, Candle, Direction

TRACKING = "TRACKING"
COMPLETED_2R = "COMPLETED_2R"
INVALIDATED = "INVALIDATED"
AMBIGUOUS_SAME_BAR = "AMBIGUOUS_SAME_BAR"
EXPIRED = "EXPIRED"
DATA_GAP = "DATA_GAP"
TERMINAL_STATUSES = (COMPLETED_2R, INVALIDATED, AMBIGUOUS_SAME_BAR, EXPIRED, DATA_GAP)

PENDING = "PENDING"
TARGET_FIRST = "TARGET_FIRST"
INVALIDATION_FIRST = "INVALIDATION_FIRST"
AMBIGUOUS = "AMBIGUOUS"
UNRESOLVED = "UNRESOLVED"  # Horizon ended before the target or invalidation.

TARGETS: tuple[tuple[str, Decimal], ...] = (
    ("0_5r", Decimal("0.5")),
    ("1r", Decimal(1)),
    ("1_5r", Decimal("1.5")),
    ("2r", Decimal(2)),
)
KEYS = tuple(key for key, _ in TARGETS)
TARGET_LABELS = {"0_5r": "+0.5R", "1r": "+1R", "1_5r": "+1.5R", "2r": "+2R"}
LOWER_TIMEFRAME = {"15m": "5m", "1h": "15m", "4h": "1h"}
QUANTUM = Decimal("1e-12")  # Matches Numeric(30, 12) so stored prices equal computed.


@dataclass(frozen=True)
class Plan:
    """Frozen at READY."""

    direction: Direction
    timeframe: str
    entry: Decimal
    invalidation: Decimal
    risk: Decimal
    targets: dict[str, Decimal]


@dataclass
class Progress:
    """Mutable tracking state; field names match SetupOutcome columns."""

    last_processed_at: datetime
    max_favorable_price: Decimal
    max_adverse_price: Decimal
    hits: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(KEYS, False))
    first: dict[str, str] = field(default_factory=lambda: dict.fromkeys(KEYS, PENDING))
    bars_to: dict[str, int | None] = field(
        default_factory=lambda: dict.fromkeys(KEYS, None)
    )
    hit_minus_1r: bool = False
    bars_to_invalidation: int | None = None
    first_event: str | None = None
    first_event_at: datetime | None = None
    max_favorable_excursion_r: float = 0.0
    max_adverse_excursion_r: float = 0.0
    bars_processed: int = 0
    ambiguity_resolution: str | None = None
    expired_at: datetime | None = None
    completed_at: datetime | None = None
    outcome_status: str = TRACKING

    def copy(self) -> Progress:
        return replace(
            self,
            hits=dict(self.hits),
            first=dict(self.first),
            bars_to=dict(self.bars_to),
        )


@dataclass
class Advance:
    progress: Progress
    # A parent bar that stayed ambiguous for lack of lower-timeframe candles.
    unresolved_parent: Candle | None = None


def build_plan(
    direction: Direction, timeframe: str, entry: Decimal, invalidation: Decimal
) -> Plan | None:
    """Targets from risk frozen at READY. Returns None when risk is not positive."""
    entry, invalidation = entry.quantize(QUANTUM), invalidation.quantize(QUANTUM)
    sign = 1 if direction == Direction.LONG else -1
    risk = (entry - invalidation) * sign
    if risk <= 0:
        return None
    targets = {
        key: (entry + sign * multiple * risk).quantize(QUANTUM)
        for key, multiple in TARGETS
    }
    return Plan(direction, timeframe, entry, invalidation, risk, targets)


def plan_from_row(row: Any, direction: Direction) -> Plan:
    return Plan(
        direction=direction,
        timeframe=row.timeframe,
        entry=Decimal(row.entry_reference_price),
        invalidation=Decimal(row.invalidation_price),
        risk=Decimal(row.initial_risk_distance),
        targets={key: Decimal(getattr(row, f"target_{key}_price")) for key in KEYS},
    )


def _utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; all stored times are UTC.
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def progress_from_row(row: Any) -> Progress:
    cursor = _utc(row.last_processed_at)
    assert cursor is not None
    return Progress(
        last_processed_at=cursor,
        max_favorable_price=Decimal(row.max_favorable_price),
        max_adverse_price=Decimal(row.max_adverse_price),
        hits={key: bool(getattr(row, f"hit_{key}")) for key in KEYS},
        first={key: getattr(row, f"first_{key}") for key in KEYS},
        bars_to={key: getattr(row, f"bars_to_{key}") for key in KEYS},
        hit_minus_1r=bool(row.hit_minus_1r),
        bars_to_invalidation=row.bars_to_invalidation,
        first_event=row.first_event,
        first_event_at=_utc(row.first_event_at),
        max_favorable_excursion_r=row.max_favorable_excursion_r,
        max_adverse_excursion_r=row.max_adverse_excursion_r,
        bars_processed=row.bars_processed,
        ambiguity_resolution=row.ambiguity_resolution,
        expired_at=_utc(row.expired_at),
        completed_at=_utc(row.completed_at),
        outcome_status=row.outcome_status,
    )


def progress_columns(progress: Progress) -> dict[str, Any]:
    values: dict[str, Any] = {
        name: getattr(progress, name)
        for name in (
            "hit_minus_1r",
            "bars_to_invalidation",
            "first_event",
            "first_event_at",
            "max_favorable_price",
            "max_adverse_price",
            "max_favorable_excursion_r",
            "max_adverse_excursion_r",
            "bars_processed",
            "last_processed_at",
            "ambiguity_resolution",
            "expired_at",
            "completed_at",
            "outcome_status",
        )
    }
    for key in KEYS:
        values[f"hit_{key}"] = progress.hits[key]
        values[f"first_{key}"] = progress.first[key]
        values[f"bars_to_{key}"] = progress.bars_to[key]
    return values


def valid_children(parent: Candle, children: list[Candle], timeframe: str) -> bool:
    """Children must exactly tile the completed parent and reproduce its range."""
    child_seconds = INTERVAL_SECONDS[LOWER_TIMEFRAME[timeframe]]  # type: ignore[index]
    expected = INTERVAL_SECONDS[timeframe] // child_seconds  # type: ignore[index]
    if len(children) != expected:
        return False
    for index, child in enumerate(children):
        if child.open_time != parent.open_time + timedelta(
            seconds=index * child_seconds
        ):
            return False
        if child.close_time > parent.close_time:
            return False
    return (
        children[-1].close_time == parent.close_time
        and max(c.high for c in children) == parent.high
        and min(c.low for c in children) == parent.low
    )


def advance(
    plan: Plan,
    progress: Progress,
    bars: list[Candle],
    now: datetime,
    max_bars: int,
    children: dict[datetime, list[Candle]] | None = None,
) -> Advance:
    """Apply newly CLOSED bars after the cursor. Idempotent for already-seen bars."""
    state = progress.copy()
    result = Advance(state)
    if state.outcome_status != TRACKING:
        return result
    long = plan.direction == Direction.LONG
    fresh = sorted(
        (
            b
            for b in bars
            if b.close_time > state.last_processed_at and b.close_time <= now
        ),
        key=lambda b: b.open_time,
    )
    for bar in fresh:
        # The next bar must open right after the cursor (close times end 1 ms early).
        if (bar.open_time - state.last_processed_at).total_seconds() > 1:
            state.outcome_status = DATA_GAP
            state.completed_at = bar.open_time
            return result
        state.bars_processed += 1
        n = state.bars_processed
        # The whole bar range counts, including a terminal bar's range.
        if long:
            state.max_favorable_price = max(state.max_favorable_price, bar.high)
            state.max_adverse_price = min(state.max_adverse_price, bar.low)
        else:
            state.max_favorable_price = min(state.max_favorable_price, bar.low)
            state.max_adverse_price = max(state.max_adverse_price, bar.high)
        sign = 1 if long else -1
        state.max_favorable_excursion_r = max(
            0.0, float((state.max_favorable_price - plan.entry) * sign / plan.risk)
        )
        state.max_adverse_excursion_r = min(
            0.0, float((state.max_adverse_price - plan.entry) * sign / plan.risk)
        )
        stop = _touches_stop(plan, bar)
        pending = [k for k in KEYS if state.first[k] == PENDING]
        touched = [k for k in pending if _touches(plan, bar, k)]
        for key in touched:
            state.hits[key] = True
            state.bars_to[key] = n
        if stop:
            state.hit_minus_1r = True
            state.bars_to_invalidation = n
        if stop and touched:
            kids = (children or {}).get(bar.open_time)
            replay = (
                _replay_children(plan, kids, pending, touched)
                if kids is not None and valid_children(bar, kids, plan.timeframe)
                else None
            )
            if replay is not None:
                resolution, events = replay
                state.first.update(resolution)
                for event, at in events:
                    _first_event(state, event, at)
                state.ambiguity_resolution = "LOWER_TIMEFRAME"
            else:
                for key in pending:
                    state.first[key] = (
                        AMBIGUOUS if key in touched else INVALIDATION_FIRST
                    )
                _first_event(state, AMBIGUOUS_SAME_BAR, bar.close_time)
                if kids is None:
                    result.unresolved_parent = bar
                else:
                    state.ambiguity_resolution = "LOWER_TIMEFRAME_UNUSABLE"
        elif stop:
            for key in pending:
                state.first[key] = INVALIDATION_FIRST
            _first_event(state, "INVALIDATION", bar.close_time)
        elif touched:
            for key in touched:
                state.first[key] = TARGET_FIRST
            _first_event(state, "TARGET_" + touched[0].upper(), bar.close_time)
        state.last_processed_at = bar.close_time
        if stop or state.first["2r"] == TARGET_FIRST:
            state.outcome_status = (
                COMPLETED_2R
                if state.first["2r"] == TARGET_FIRST
                else AMBIGUOUS_SAME_BAR
                if AMBIGUOUS in state.first.values()
                else INVALIDATED
            )
            state.completed_at = bar.close_time
            return result
        if n >= max_bars:
            for key in KEYS:
                if state.first[key] == PENDING:
                    state.first[key] = UNRESOLVED
            state.outcome_status = EXPIRED
            state.expired_at = state.completed_at = bar.close_time
            return result
    return result


def _touches(plan: Plan, bar: Candle, key: str) -> bool:
    target = plan.targets[key]
    return bar.high >= target if plan.direction == Direction.LONG else bar.low <= target


def _touches_stop(plan: Plan, bar: Candle) -> bool:
    if plan.direction == Direction.LONG:
        return bar.low <= plan.invalidation
    return bar.high >= plan.invalidation


def _first_event(state: Progress, event: str, at: datetime) -> None:
    if state.first_event is None:
        state.first_event, state.first_event_at = event, at


def _replay_children(
    plan: Plan, children: list[Candle], pending: list[str], touched: list[str]
) -> tuple[dict[str, str], list[tuple[str, datetime]]] | None:
    """Replay an ambiguous parent through its complete lower-timeframe bars.

    Returns None (stay ambiguous) if the children never reach invalidation, which
    would contradict the parent. A child touching both sides stays ambiguous.
    """
    resolution: dict[str, str] = {}
    events: list[tuple[str, datetime]] = []
    for child in children:
        open_keys = [k for k in pending if k not in resolution]
        hit = [k for k in open_keys if k in touched and _touches(plan, child, k)]
        if _touches_stop(plan, child):
            for key in open_keys:
                resolution[key] = AMBIGUOUS if key in hit else INVALIDATION_FIRST
            events.append(
                (AMBIGUOUS_SAME_BAR if hit else "INVALIDATION", child.close_time)
            )
            return resolution, events
        for key in hit:
            resolution[key] = TARGET_FIRST
        if hit:
            events.append(("TARGET_" + hit[0].upper(), child.close_time))
    return None
