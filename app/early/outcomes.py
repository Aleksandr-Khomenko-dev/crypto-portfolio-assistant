"""Early-event outcome research (pure), kept separate from setup outcomes.

Causality: an event is classified by the engine with data available at its
timestamp only. Outcomes use ONLY candles that open at or after the event time
(the candle containing the event is skipped: its intrabar order is unknown), and the
event chain uses only LATER events. Nothing here feeds back into detection or scores.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from app.early.model import EarlyEvent, EventType
from app.scanner.domain import Candle

TARGET_FIRST, INVALIDATION_FIRST = "TARGET_FIRST", "INVALIDATION_FIRST"
AMBIGUOUS, UNRESOLVED = "AMBIGUOUS", "UNRESOLVED"

# What each event is expected to lead to (same symbol + direction; same breakout
# episode for episode events). Used for transition rates, never for detection.
SUCCESSORS: dict[str, tuple[str, ...]] = {
    "ZONE_WATCH": ("BULLISH_IGNITION", "BEARISH_IGNITION"),
    "BULLISH_IGNITION": ("FIRST_BREAK",),
    "BEARISH_IGNITION": ("FIRST_BREAK",),
    "FORMATION_WATCH": ("FIRST_BREAK",),
    "BREAKOUT_APPROACH": ("FIRST_BREAK",),
    "FIRST_BREAK": ("RETEST_CONFIRMED", "FAILED_BREAKOUT"),
    "RETEST_WATCH": ("RETEST_CONFIRMED", "FAILED_BREAKOUT"),
}
EPISODE_CHAIN = {"FIRST_BREAK", "RETEST_WATCH"}


def invalidation_for(event: EarlyEvent) -> float | None:
    """Structural invalidation known AT the event (for R multiples); None = no R."""
    if "invalidation" in event.metrics:
        return float(event.metrics["invalidation"])
    zone, sign = event.zone, 1 if event.direction == "LONG" else -1
    if event.pattern is not None and event.pattern.invalidation_level is not None:
        return event.pattern.invalidation_level
    if zone is None or zone.atr <= 0:
        return None
    if event.event_type in (EventType.BULLISH_IGNITION, EventType.BEARISH_IGNITION):
        edge = zone.lower if sign > 0 else zone.upper
        return edge - sign * 0.1 * zone.atr
    if event.event_type == EventType.FIRST_BREAK and event.episode is not None:
        return event.episode.level - sign * 0.25 * zone.atr
    return None


@dataclass
class Outcome:
    bars_used: int
    mfe_pct: float | None
    mae_pct: float | None
    r_value: float | None
    mfe_r: float | None
    mae_r: float | None
    first_1r: str
    first_2r: str
    minutes_to_1r: float | None
    minutes_to_invalidation: float | None
    continuation: bool | None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def measure(
    direction: str,
    entry: float,
    invalidation: float | None,
    detected_at: datetime,
    bars: list[Candle],
) -> Outcome:
    sign = 1 if direction == "LONG" else -1
    after = sorted(
        (b for b in bars if b.open_time >= detected_at), key=lambda b: b.open_time
    )
    if not after or entry <= 0:
        return Outcome(
            0, None, None, None, None, None, UNRESOLVED, UNRESOLVED, None, None, None
        )
    favourable = max(
        sign * (float(b.high if sign > 0 else b.low) - entry) for b in after
    )
    adverse = max(sign * (entry - float(b.low if sign > 0 else b.high)) for b in after)
    favourable, adverse = max(favourable, 0.0), max(adverse, 0.0)
    risk = (
        sign * (entry - invalidation)
        if invalidation is not None and sign * (entry - invalidation) > 0
        else None
    )
    first = {1: UNRESOLVED, 2: UNRESOLVED}
    to_1r = to_invalid = None
    if risk is not None:
        for bar in after:
            minutes = (bar.close_time - detected_at).total_seconds() / 60
            best = float(bar.high if sign > 0 else bar.low)
            worst = float(bar.low if sign > 0 else bar.high)
            invalid = sign * (worst - invalidation) <= 0  # type: ignore[operator]
            if invalid and to_invalid is None:
                to_invalid = round(minutes, 1)
            for multiple in (1, 2):
                if first[multiple] != UNRESOLVED:
                    continue
                hit = sign * (best - (entry + sign * multiple * risk)) >= 0
                if hit and invalid:
                    first[multiple] = AMBIGUOUS  # same bar: order unknown, not a win
                elif hit:
                    first[multiple] = TARGET_FIRST
                    if multiple == 1:
                        to_1r = round(minutes, 1)
                elif invalid:
                    first[multiple] = INVALIDATION_FIRST
            if invalid:
                break
    final = float(after[-1].close)
    return Outcome(
        bars_used=len(after),
        mfe_pct=round(favourable / entry * 100, 3),
        mae_pct=round(adverse / entry * 100, 3),
        r_value=risk,
        mfe_r=round(favourable / risk, 3) if risk else None,
        mae_r=round(adverse / risk, 3) if risk else None,
        first_1r=first[1],
        first_2r=first[2],
        minutes_to_1r=to_1r,
        minutes_to_invalidation=to_invalid,
        continuation=sign * (final - entry) > 0 and favourable > adverse,
    )


@dataclass(frozen=True)
class EventRecord:
    id: str
    symbol: str
    direction: str
    event_type: str
    detected_at: datetime
    episode_id: str | None


def successor(
    event: EventRecord, later: list[EventRecord], horizon_minutes: float
) -> tuple[str | None, float | None]:
    """First expected follow-up event strictly after `event` within the horizon."""
    wanted = SUCCESSORS.get(event.event_type, ())
    for other in sorted(later, key=lambda e: e.detected_at):
        minutes = (other.detected_at - event.detected_at).total_seconds() / 60
        if minutes <= 0 or minutes > horizon_minutes:
            continue
        if (
            other.symbol == event.symbol
            and other.event_type in wanted
            and (other.direction == event.direction or event.event_type == "ZONE_WATCH")
            and (
                event.event_type not in EPISODE_CHAIN
                or other.episode_id == event.episode_id
            )
        ):
            return other.event_type, round(minutes, 1)
    return None, None


def _rate(numerator: int, denominator: int, enough: bool) -> float | None:
    return round(numerator / denominator, 3) if enough and denominator else None


def calibration(rows: list[dict[str, Any]], min_samples: int) -> dict[str, Any]:
    """Measured statistics per event type. Rates are withheld below `min_samples`
    (reported as None with a LOW_SAMPLE warning), and nothing is a probability claim."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["event_type"]].append(row)
    report = {}
    for event_type, items in sorted(grouped.items()):
        n = len(items)
        enough = n >= min_samples
        resolved_1r = [
            r for r in items if r["first_1r"] in (TARGET_FIRST, INVALIDATION_FIRST)
        ]
        resolved_2r = [
            r for r in items if r["first_2r"] in (TARGET_FIRST, INVALIDATION_FIRST)
        ]
        successors = [r for r in items if r.get("next_event_type")]
        failed = [r for r in items if r.get("next_event_type") == "FAILED_BREAKOUT"]
        continued = [r for r in items if r.get("continuation") is not None]

        def med(
            key: str, source: list[dict[str, Any]] = items, enough: bool = enough
        ) -> float | None:
            values = [r[key] for r in source if r.get(key) is not None]
            return round(median(values), 3) if values and enough else None

        report[event_type] = {
            "sample": n,
            "warning": None
            if enough
            else f"LOW_SAMPLE (<{min_samples}); rates withheld",
            "followed_by_expected_event": _rate(len(successors), n, enough),
            "successor_counts": dict(
                sorted(
                    {
                        t: sum(r.get("next_event_type") == t for r in items)
                        for t in SUCCESSORS.get(event_type, ())
                    }.items()
                )
            ),
            "plus_1r_before_invalidation": _rate(
                sum(r["first_1r"] == TARGET_FIRST for r in resolved_1r),
                len(resolved_1r),
                enough,
            ),
            "plus_2r_before_invalidation": _rate(
                sum(r["first_2r"] == TARGET_FIRST for r in resolved_2r),
                len(resolved_2r),
                enough,
            ),
            "r_resolved_1r": len(resolved_1r),
            "ambiguous_1r": sum(r["first_1r"] == AMBIGUOUS for r in items),
            "false_breakout_rate": _rate(len(failed), n, enough)
            if event_type in EPISODE_CHAIN
            else None,
            "continuation_rate": _rate(
                sum(bool(r["continuation"]) for r in continued), len(continued), enough
            ),
            "median_mfe_pct": med("mfe_pct"),
            "median_mae_pct": med("mae_pct"),
            "median_mfe_r": med("mfe_r"),
            "median_mae_r": med("mae_r"),
            "median_minutes_to_next_event": med("minutes_to_next_event", successors),
            "median_minutes_to_1r": med("minutes_to_1r"),
            "median_minutes_to_invalidation": med("minutes_to_invalidation"),
        }
    return report
