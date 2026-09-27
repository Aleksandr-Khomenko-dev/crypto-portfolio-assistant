"""Types for the early-warning engine. Strengths are diagnostics, never probabilities."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    ZONE_WATCH = "ZONE_WATCH"
    BULLISH_IGNITION = "BULLISH_IGNITION"
    BEARISH_IGNITION = "BEARISH_IGNITION"
    FORMATION_WATCH = "FORMATION_WATCH"
    BREAKOUT_APPROACH = "BREAKOUT_APPROACH"
    FIRST_BREAK = "FIRST_BREAK"
    FAST_MOVE = "FAST_MOVE"
    MOMENTUM_CONFIRMED = "MOMENTUM_CONFIRMED"
    RETEST_WATCH = "RETEST_WATCH"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    EXTREME_MOVE = "EXTREME_MOVE"
    LATE_EXTENDED_MOVE = "LATE_EXTENDED_MOVE"
    COOLED_DOWN = "COOLED_DOWN"  # episode state only; never alerted


# Outbox / Telegram priority (lower = more urgent): RETEST_CONFIRMED, FIRST_BREAK,
# RETEST_WATCH, MOMENTUM_CONFIRMED, ignition, approach, then EXTREME_MOVE. Research-
# only events (zone, formation, late) are persisted last and can never crowd out
# FIRST_BREAK / RETEST_CONFIRMED / EXTREME_MOVE under backpressure.
PRIORITY = {
    EventType.RETEST_CONFIRMED: 0,
    EventType.FIRST_BREAK: 0,
    EventType.EXTREME_MOVE: 0,
    EventType.RETEST_WATCH: 1,
    EventType.MOMENTUM_CONFIRMED: 1,
    EventType.BULLISH_IGNITION: 2,
    EventType.BEARISH_IGNITION: 2,
    EventType.BREAKOUT_APPROACH: 2,
    EventType.FAILED_BREAKOUT: 2,
    EventType.FAST_MOVE: 3,
    EventType.LATE_EXTENDED_MOVE: 4,
    EventType.ZONE_WATCH: 4,
    EventType.FORMATION_WATCH: 4,
}
LOW_PRIORITY = {EventType.ZONE_WATCH, EventType.FORMATION_WATCH}
# Events that trigger a closed-candle PRIORITY ANALYSIS of that one symbol.
RESCAN_EVENTS = {
    EventType.BULLISH_IGNITION,
    EventType.BEARISH_IGNITION,
    EventType.BREAKOUT_APPROACH,
    EventType.FIRST_BREAK,
    EventType.FAST_MOVE,
    EventType.RETEST_WATCH,
}
# Closed-candle scanner states: early events must never overwrite these.
SCANNER_STATES = {"WATCH", "SETUP_FORMING", "HIGH_CONFLUENCE", "EXTREME_CONFLUENCE"}


@dataclass(frozen=True)
class EarlyConfig:
    """Conservative research defaults (every one is a CPDA_EARLY_* setting).

    Distances are in ATR of the zone's reference timeframe: 1H ATR for 1H/4H zones,
    15m ATR for 15m zones and 15m patterns, 5m ATR for 5m patterns."""

    zone_min_touches: int = (
        2  # S/R zones need repeated interaction (break levels exempt)
    )
    zones_per_side: int = 3  # nearest zones per side and timeframe
    zone_width_atr: float = 0.3  # half-width of a previous-breakout-level zone
    zone_watch_atr: float = 0.25  # "inside" tolerance around a zone
    zone_arrival_atr: float = 0.75  # price must ARRIVE from at least this far away
    approach_atr: float = 0.35  # BREAKOUT_APPROACH when this close, not yet broken
    approach_min_evidence: int = 1
    approach_notify_atr: float = 0.20  # close band (Telegram policy threshold)
    break_atr: float = 0.10  # minimum penetration (ATR) ...
    break_sigma_k: float = 2.0  # ... and >= k * realised 1m volatility ...
    break_spread_k: float = 3.0  # ... and >= k * bid/ask spread
    break_hold_seconds: float = 10.0  # beyond the level for this long ...
    break_min_samples: int = 3  # ... across at least this many stream updates
    break_prior_seconds: float = 900.0  # pre-break side must be observed live recently
    retest_departure_atr: float = 0.30  # must first move away before a retest counts
    retest_atr: float = 0.25  # retest tolerance around the broken level
    retest_confirm_atr: float = 0.20  # rejection size off the retest extreme
    retest_min_confirmations: int = 2
    retest_max_minutes: float = 120.0
    fail_atr: float = 0.25  # back inside the prior range by this much = failed
    episode_max_hours: float = 6.0
    ignition_min_strength: int = 55
    ignition_zone_atr: float = 0.5
    ignition_lookback_bars: int = 12
    ignition_max_distance_atr: float = 1.0
    ignition_fresh_bars: int = 3  # shift must be within the last 3 closed 5m bars
    volume_confirm: float = 1.5  # RVOL / realtime volume ratio counted as expansion
    momentum_volume: float = 3.0  # realtime volume anomaly for MOMENTUM_CONFIRMED
    late_origin_atr: float = 3.0  # 1H ATRs from the origin = already extended
    late_origin_lookback_bars: int = 48  # closed 5m bars (4 hours)
    # Acceptance: when price already traded around the current level (+-0.5 ATR)
    # for this many closed 5m bars, it is a consolidation, not an extension;
    # the origin is then measured from the start of that acceptance.
    late_acceptance_bars: int = 12
    late_acceptance_atr: float = 0.5
    pattern_lookback_bars: int = 40
    pattern_tolerance_atr: float = 0.25
    pattern_compression_ratio: float = 0.6
    pattern_level_atr: float = 0.6
    flag_impulse_atr: float = 3.0
    # Alerts: FORMATION_WATCH only for these timeframes (5m candidates remain
    # internal evidence); a formation boundary drives realtime break/retest
    # events only when well-formed.
    formation_alert_timeframes: tuple[str, ...] = ("15m",)
    pattern_break_min_strength: int = 70
    cooldown_minutes: float = 120.0
    upgrade_strength_delta: int = 15
    upgrade_min_seconds: float = 300.0  # an upgrade never follows within 5 min
    context_max_age_minutes: float = 15.0
    min_live_seconds: float = 60.0  # realtime history needed before any event


@dataclass(frozen=True)
class KeyZone:
    timeframe: str  # 4h / 1h / 15m / 5m
    kind: str  # SUPPORT / RESISTANCE (relative to price when the context was built)
    lower: float
    upper: float
    created_at: datetime | None
    touches: int
    atr: float  # reference ATR used for every distance to this zone
    source: str = "SR"  # SR | BREAK_LEVEL | PATTERN

    @property
    def key(self) -> str:
        return f"{self.timeframe}:{self.source}:{self.lower:.8g}-{self.upper:.8g}"

    @property
    def htf(self) -> bool:
        return self.timeframe in ("1h", "4h")

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "kind": self.kind,
            "lower": self.lower,
            "upper": self.upper,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "touches": self.touches,
            "atr": self.atr,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyZone:
        created = data.get("created_at")
        return cls(
            timeframe=data["timeframe"],
            kind=data["kind"],
            lower=float(data["lower"]),
            upper=float(data["upper"]),
            created_at=datetime.fromisoformat(created) if created else None,
            touches=int(data.get("touches", 0)),
            atr=float(data["atr"]),
            source=data.get("source", "SR"),
        )


@dataclass
class PatternCandidate:
    pattern_type: str
    direction: str
    source_timeframe: str
    formation_started_at: datetime
    boundary_level: float
    secondary_boundary: float | None
    touch_count: int
    compression_ratio: float | None
    distance_to_trigger_atr: float
    formation_strength: int
    invalidation_condition: str
    invalidation_level: float | None
    atr: float
    evidence: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return (
            f"{self.pattern_type}:{self.source_timeframe}:{self.direction}:"
            f"{self.boundary_level:.6g}"
        )


@dataclass
class Extension:
    origin_price: float
    origin_at: datetime | None
    move_pct: float
    distance_atr: float
    late: bool


@dataclass
class BreakoutEpisode:
    """FIRST_BREAK -> RETEST_WATCH -> RETEST_CONFIRMED | FAILED_BREAKOUT -> COOLED_DOWN."""

    symbol: str
    direction: str
    zone: KeyZone
    level: float  # the broken boundary (upper for LONG, lower for SHORT)
    break_time: float  # epoch seconds
    break_price: float
    phase: str = "BROKEN"  # BROKEN | RETESTING | CONFIRMED | FAILED | COOLED_DOWN
    max_extension: float = 0.0  # price units beyond the level
    retest_started: float | None = None
    retest_extreme: float | None = None
    momentum_sent: bool = False
    late: bool = False
    origin_price: float | None = None
    updated: float = 0.0
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sign(self) -> int:
        return 1 if self.direction == "LONG" else -1

    @property
    def key(self) -> str:
        return f"{self.zone.key}:{self.direction}"

    def advance(self, phase: str, at: float) -> None:
        self.phase, self.updated = phase, at
        self.history.append({"phase": phase, "at": at})


@dataclass
class EarlyEvent:
    symbol: str
    event_type: EventType
    direction: str  # LONG / SHORT context, never an instruction
    price: float
    detected_at: float  # epoch seconds
    source_timeframe: str | None = None
    zone: KeyZone | None = None
    strength: int | None = None  # formation / ignition / momentum diagnostic 0-100
    reasons: list[str] = field(default_factory=list)  # deterministic evidence codes
    confirmations: list[str] = field(default_factory=list)  # human-readable (ru)
    metrics: dict[str, Any] = field(default_factory=dict)
    episode: BreakoutEpisode | None = None
    pattern: PatternCandidate | None = None
    key: str = ""  # anti-spam episode key
    decision: str = "NEW"

    @property
    def priority(self) -> int:
        return PRIORITY[self.event_type]
