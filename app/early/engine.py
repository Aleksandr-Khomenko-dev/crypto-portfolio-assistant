"""Early event engine: closed-candle context + realtime prices -> early events (pure).

Two entry points, both deterministic and free of I/O:

* `set_context` (every closed 5m candle): ignition and formation candidates.
* `on_tick` (realtime stream): zone watch, breakout approach, first break and the
  breakout episode (retest watch / retest confirmed / failed breakout / momentum).

Realtime observations never flow back into the closed-candle context or the scanner.
Late-move protection relabels anything already far from its origin as
LATE_EXTENDED_MOVE instead of a fresh early event.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace

from app.early.context import StructureContext, extension
from app.early.ignition import ignition
from app.early.model import (
    BreakoutEpisode,
    EarlyConfig,
    EarlyEvent,
    EventType,
    KeyZone,
    PatternCandidate,
)
from app.fast.detector import FastState, SymbolTracker, Trigger

IGNITIONS = {EventType.BULLISH_IGNITION, EventType.BEARISH_IGNITION}
EPISODE_EVENTS = {
    EventType.FIRST_BREAK,
    EventType.RETEST_WATCH,
    EventType.RETEST_CONFIRMED,
    EventType.FAILED_BREAKOUT,
    EventType.MOMENTUM_CONFIRMED,
}
UPGRADABLE = {
    EventType.BULLISH_IGNITION,
    EventType.BEARISH_IGNITION,
    EventType.FORMATION_WATCH,
    EventType.BREAKOUT_APPROACH,
}
# A static level property (repeated tests, 15m OI) is never enough for an approach:
# something must be happening now.
DYNAMIC_EVIDENCE = {"COMPRESSION", "VOLUME_RISING", "MOMENTUM_TO_LEVEL"}
APPROACH_KEY = "approach"
# A retest is confirmed by what price/volume DO at the level, not by static context
# (15m OI, BTC/ETH regime): at least one of these is required.
DYNAMIC_RETEST = {"MICRO_BOS_1M", "VOLUME_RENEWED"}
FAST_TYPES = {
    FastState.FAST_MOVE: EventType.FAST_MOVE,
    FastState.CONFIRMED_MOMENTUM: EventType.MOMENTUM_CONFIRMED,
    FastState.EXTREME_MOVE: EventType.EXTREME_MOVE,
}


@dataclass
class _Sent:
    at: float
    strength: int | None
    distance: float | None
    upgrades: int = 0


class EarlyAntiSpam:
    """DETECTION dedupe: one record per symbol + direction + event type + episode
    key, re-recorded only on a material upgrade (strength +delta, approach distance
    halved; one upgrade, never within 5 min) or after the cooldown. Whether the user
    is told is decided separately by `app.early.policy`."""

    def __init__(self, cfg: EarlyConfig) -> None:
        self.cfg = cfg
        self.sent: dict[tuple[str, str, str, str], _Sent] = {}
        self.counters: Counter[str] = Counter()
        self.duplicates: Counter[str] = Counter()  # per event type

    def cooldown(self, event_type: EventType) -> float:
        if event_type in EPISODE_EVENTS or event_type == EventType.LATE_EXTENDED_MOVE:
            return self.cfg.episode_max_hours * 3600
        return self.cfg.cooldown_minutes * 60

    def decide(self, event: EarlyEvent, now: float) -> str | None:
        k = (event.symbol, event.direction, event.event_type.value, event.key)
        previous = self.sent.get(k)
        distance = event.metrics.get("distance_to_level_atr")
        if previous is not None and now - previous.at <= self.cooldown(
            event.event_type
        ):
            stronger = (
                event.strength is not None
                and previous.strength is not None
                and event.strength
                >= previous.strength + self.cfg.upgrade_strength_delta
            )
            closer = (
                event.event_type == EventType.BREAKOUT_APPROACH
                and distance is not None
                and previous.distance
                and distance <= previous.distance * 0.5
            )
            if (
                event.event_type in UPGRADABLE
                and previous.upgrades < 1
                and now - previous.at >= self.cfg.upgrade_min_seconds
                and (stronger or closer)
            ):
                previous.upgrades += 1
                previous.strength, previous.distance = event.strength, distance
                return "UPGRADE"
            self.counters["duplicate"] += 1
            self.duplicates[event.event_type.value] += 1
            return None
        self.sent[k] = _Sent(now, event.strength, distance)
        return "NEW"

    def seed(
        self,
        symbol: str,
        direction: str,
        event_type: str,
        key: str,
        at: float,
        strength: int | None = None,
    ) -> None:
        self.sent[(symbol, direction, event_type, key)] = _Sent(at, strength, None, 1)


def _streak(tracker: SymbolTracker, beyond) -> tuple[int, float | None]:
    """Consecutive most-recent samples satisfying `beyond(price)` and the first time."""
    count, start = 0, None
    for at, price in reversed(tracker.samples):
        if not beyond(price):
            break
        count, start = count + 1, at
    return count, start


def _change(
    tracker: SymbolTracker, now: float, seconds: float, price: float
) -> float | None:
    start = tracker.price_at(now - seconds)
    return round((price / start - 1) * 100, 3) if start else None


class EarlyEngine:
    def __init__(
        self,
        cfg: EarlyConfig,
        *,
        ignition_enabled: bool = True,
        patterns_enabled: bool = True,
        zone_watch_enabled: bool = True,
        formation_min_strength: int = 50,
    ) -> None:
        self.cfg = cfg
        self.ignition_enabled = ignition_enabled
        self.patterns_enabled = patterns_enabled
        self.zone_watch_enabled = zone_watch_enabled
        self.formation_min_strength = formation_min_strength
        self.contexts: dict[str, StructureContext] = {}
        self.episodes: dict[str, dict[str, BreakoutEpisode]] = defaultdict(dict)
        self.pre_side: dict[str, dict[str, float]] = defaultdict(dict)
        self.recent_fast: dict[tuple[str, str], float] = {}
        self.antispam = EarlyAntiSpam(cfg)
        self.counters: Counter[str] = Counter()
        self.finished: list[BreakoutEpisode] = []  # cooled down; to persist
        self.pattern_status: list[tuple[str, PatternCandidate, str]] = []
        self.active_patterns: dict[str, dict[str, PatternCandidate]] = defaultdict(dict)

    # ---- helpers --------------------------------------------------------------------

    def _gate(self, events: Iterable[EarlyEvent], now: float) -> list[EarlyEvent]:
        allowed = []
        for event in events:
            decision = self.antispam.decide(event, now)
            if decision is None:
                self.counters["early_events_suppressed"] += 1
                continue
            event.decision = decision
            self.counters[event.event_type.value.lower() + "_events"] += 1
            allowed.append(event)
        return allowed

    def _late(self, event: EarlyEvent, ctx: StructureContext) -> EarlyEvent:
        """Relabel an event whose move already travelled too far from its origin."""
        ext = extension(ctx, event.price, event.direction, self.cfg)
        if ext is None:
            return event
        event.metrics.update(
            distance_from_origin_atr=ext.distance_atr,
            move_since_origin_pct=ext.move_pct,
            origin_price=ext.origin_price,
            origin_at=ext.origin_at.isoformat() if ext.origin_at else None,
        )
        if not ext.late:
            return event
        self.counters["late_extended_relabels"] += 1
        return replace(
            event,
            event_type=EventType.LATE_EXTENDED_MOVE,
            reasons=[f"LATE_AFTER_{event.event_type.value}", *event.reasons],
            key="",  # at most once per symbol + direction per episode window
            metrics={**event.metrics, "original_event": event.event_type.value},
        )

    @staticmethod
    def _pattern_zone(pattern: PatternCandidate) -> KeyZone:
        return KeyZone(
            timeframe=pattern.source_timeframe,
            kind="RESISTANCE" if pattern.direction == "LONG" else "SUPPORT",
            lower=pattern.boundary_level,
            upper=pattern.boundary_level,
            created_at=pattern.formation_started_at,
            touches=pattern.touch_count,
            atr=pattern.atr,
            source="PATTERN",
        )

    # ---- closed-candle path ---------------------------------------------------------

    def set_context(
        self,
        ctx: StructureContext,
        price: float | None,
        now: float,
        prime: bool = False,
        defer: bool = False,
    ) -> list[EarlyEvent]:
        """New closed 5m context. `prime` (first pass after start) records current
        candidates silently so a restart never replays them as fresh events.
        `defer` returns candidates ungated so a caller can rank a whole pass
        (see `gate_pass`)."""
        symbol = ctx.symbol
        self.contexts[symbol] = ctx
        current = (
            price
            if price is not None
            else float(ctx.bars5[-1].close)
            if ctx.bars5
            else None
        )
        if current is None:
            return []
        events: list[EarlyEvent] = []
        if self.ignition_enabled:
            for direction in ("LONG", "SHORT"):
                found = ignition(ctx, direction, current, now, self.cfg)
                if found is not None:
                    events.append(self._late(found, ctx))
        seen = {}
        for pattern in ctx.patterns:
            seen[pattern.key] = pattern
            if (
                not self.patterns_enabled
                or pattern.formation_strength < self.formation_min_strength
                or pattern.source_timeframe not in self.cfg.formation_alert_timeframes
            ):
                continue
            events.append(
                self._late(
                    EarlyEvent(
                        symbol=symbol,
                        event_type=EventType.FORMATION_WATCH,
                        direction=pattern.direction,
                        price=current,
                        detected_at=now,
                        source_timeframe=pattern.source_timeframe,
                        zone=self._pattern_zone(pattern),
                        strength=pattern.formation_strength,
                        reasons=[pattern.pattern_type],
                        confirmations=list(pattern.evidence),
                        metrics={
                            "distance_to_level_atr": pattern.distance_to_trigger_atr,
                            "compression_ratio": pattern.compression_ratio,
                        },
                        pattern=pattern,
                        key=pattern.key,
                    ),
                    ctx,
                )
            )
        self._update_patterns(symbol, ctx, seen)
        if prime:
            for event in events:
                self.antispam.decide(event, now)
            self.counters["primed_on_start"] += len(events)
            return []
        return events if defer else self._gate(events, now)

    def gate_pass(self, events: list[EarlyEvent], now: float) -> list[EarlyEvent]:
        """Dedupe one market-wide 5m pass, strongest candidates first (so the
        notification budget, if any, goes to the best-formed ones)."""
        return self._gate(sorted(events, key=lambda e: -(e.strength or 0)), now)

    def _update_patterns(
        self, symbol: str, ctx: StructureContext, seen: dict[str, PatternCandidate]
    ) -> None:
        last = float(ctx.bars5[-1].close) if ctx.bars5 else None
        for key, pattern in list(self.active_patterns[symbol].items()):
            level = pattern.invalidation_level
            invalid = (
                last is not None
                and level is not None
                and (last < level if pattern.direction == "LONG" else last > level)
            )
            if invalid:
                self.pattern_status.append((symbol, pattern, "INVALIDATED"))
                del self.active_patterns[symbol][key]
            elif key not in seen:
                self.pattern_status.append((symbol, pattern, "EXPIRED"))
                del self.active_patterns[symbol][key]
        for key, pattern in seen.items():
            self.active_patterns[symbol].setdefault(key, pattern)

    # ---- realtime path ---------------------------------------------------------------

    def note_fast(self, symbol: str, direction: str, now: float) -> None:
        self.recent_fast[(symbol, direction)] = now

    def classify_fast(
        self, symbol: str, trigger: Trigger, now: float
    ) -> tuple[EventType, list[str], dict]:
        """Early-event label for a fast move; late moves are never called early."""
        event_type = FAST_TYPES.get(trigger.state, EventType.FAST_MOVE)
        reasons: list[str] = []
        metrics: dict = {}
        if any(
            ep.direction == trigger.direction and now - ep.break_time <= 900
            for ep in self.episodes.get(symbol, {}).values()
        ):
            reasons.append("BREAKOUT_ACCELERATION")
        if "VOLUME_EXPANSION" in trigger.reasons:
            reasons.append("PRICE_PLUS_VOLUME")
        ctx = self.contexts.get(symbol)
        if ctx is not None:
            ext = extension(ctx, trigger.price, trigger.direction, self.cfg)
            if ext is not None:
                metrics = {
                    "distance_from_origin_atr": ext.distance_atr,
                    "move_since_origin_pct": ext.move_pct,
                    "origin_price": ext.origin_price,
                }
                if ext.late:
                    event_type = EventType.LATE_EXTENDED_MOVE
                    metrics["original_event"] = FAST_TYPES.get(
                        trigger.state, EventType.FAST_MOVE
                    ).value
                    self.counters["late_extended_events"] += 1
        return event_type, reasons, metrics

    def on_tick(
        self, symbol: str, tracker: SymbolTracker, now: float
    ) -> list[EarlyEvent]:
        ctx = self.contexts.get(symbol)
        if ctx is None or not tracker.samples:
            return []
        if now - ctx.built_at.timestamp() > self.cfg.context_max_age_minutes * 60:
            self.counters["context_stale"] += 1
            return []
        if now - tracker.samples[0][0] < self.cfg.min_live_seconds:
            return []  # warming up: not enough live history to judge a transition
        price = tracker.samples[-1][1]
        events = self._progress(symbol, ctx, tracker, price, now)
        # Realtime alerts only at meaningful levels: 1H/4H zones and previous
        # breakout levels, plus 15m formation boundaries. 15m S/R and 5m patterns
        # stay closed-candle context (live: they produced most of the noise).
        zones = [z for z in ctx.zones if z.htf] + [
            self._pattern_zone(p)
            for p in ctx.patterns
            if p.source_timeframe == "15m"
            and p.formation_strength >= self.cfg.pattern_break_min_strength
        ]
        for zone in zones:
            events += self._zone_events(symbol, ctx, tracker, zone, price, now)
        return self._gate(events, now)

    def _penetration_floor(
        self, ctx: StructureContext, tracker: SymbolTracker, ref: float, price: float
    ) -> float:
        sigma = tracker.sigma_1m_pct()
        floor = self.cfg.break_atr * ref
        if sigma is not None:
            floor = max(floor, self.cfg.break_sigma_k * sigma / 100 * price)
        if ctx.spread_pct is not None:
            floor = max(floor, self.cfg.break_spread_k * ctx.spread_pct / 100 * price)
        return floor

    def _zone_events(
        self,
        symbol: str,
        ctx: StructureContext,
        tracker: SymbolTracker,
        zone: KeyZone,
        price: float,
        now: float,
    ) -> list[EarlyEvent]:
        cfg, ref = self.cfg, zone.atr
        if ref <= 0:
            return []
        long = zone.kind == "RESISTANCE"  # breaking resistance is LONG context
        direction = "LONG" if long else "SHORT"
        sign = 1 if long else -1
        level = zone.upper if long else zone.lower  # broken boundary
        near = zone.lower if long else zone.upper  # approached boundary
        side_key = f"{zone.key}:{direction}"
        if sign * (price - level) <= 0:
            self.pre_side[symbol][side_key] = now
        # Arrival must be SEEN LIVE: price was at least `zone_arrival_atr` away from the
        # near edge recently. Closed history alone never counts (no restart replays).
        far_key = side_key + ":far"
        if sign * (near - price) >= cfg.zone_arrival_atr * ref:
            self.pre_side[symbol][far_key] = now
        seen_far = self.pre_side[symbol].get(far_key)
        arrived_live = (
            seen_far is not None and now - seen_far <= cfg.break_prior_seconds
        )
        events: list[EarlyEvent] = []
        base = {
            "zone_timeframe": zone.timeframe,
            "zone_source": zone.source,
            "velocity_1m_pct": _change(tracker, now, 60, price),
            "move_5m_pct": _change(tracker, now, 300, price),
            "volume_ratio": _round(tracker.volume_ratio()),
            "oi_change_15m_pct": ctx.oi_change_15m,
        }
        # FIRST_BREAK: sustained, adaptive penetration seen transitioning live.
        penetration = sign * (price - level)
        seen_before = self.pre_side[symbol].get(side_key)
        recent_break = any(
            ep.direction == direction and now - ep.break_time <= 300
            for ep in self.episodes[symbol].values()
        )
        if (
            penetration > 0
            and side_key not in self.episodes[symbol]
            and seen_before is not None
            and now - seen_before <= cfg.break_prior_seconds
            and not recent_break
        ):
            floor = self._penetration_floor(ctx, tracker, ref, price)
            count, start = _streak(tracker, lambda p: sign * (p - level) > 0)
            if (
                penetration >= floor
                and count >= cfg.break_min_samples
                and start is not None
                and now - start >= cfg.break_hold_seconds
            ):
                episode = BreakoutEpisode(
                    symbol, direction, zone, level, start, price, updated=now
                )
                episode.advance("BROKEN", now)
                event = self._late(
                    EarlyEvent(
                        symbol=symbol,
                        event_type=EventType.FIRST_BREAK,
                        direction=direction,
                        price=price,
                        detected_at=now,
                        source_timeframe=zone.timeframe,
                        zone=zone,
                        reasons=[
                            "INTRABAR_BREAK",
                            f"{zone.timeframe.upper()}_{zone.source}",
                        ],
                        metrics={
                            **base,
                            "penetration_pct": round(penetration / level * 100, 3),
                            "penetration_atr": round(penetration / ref, 3),
                            "penetration_floor_atr": round(floor / ref, 3),
                            "break_started_at": start,
                            "hold_seconds": round(now - start, 1),
                        },
                        episode=episode,
                    ),
                    ctx,
                )
                episode.late = event.event_type == EventType.LATE_EXTENDED_MOVE
                episode.origin_price = event.metrics.get("origin_price")
                if event.event_type == EventType.FIRST_BREAK:
                    event.key = str(episode.id)  # once per breakout episode
                self.episodes[symbol][side_key] = episode
                if zone.source == "PATTERN":
                    for pattern in ctx.patterns:
                        if self._pattern_zone(pattern).key == zone.key:
                            self.pattern_status.append((symbol, pattern, "TRIGGERED"))
                events.append(event)
                return events
        # BREAKOUT_APPROACH: close to the near edge, arrived from farther away.
        gap = sign * (near - price)
        distance = gap / ref
        if 0 < distance <= cfg.approach_atr and arrived_live:
            evidence = self._approach_evidence(
                ctx, tracker, zone, direction, price, now
            )
            dynamic = [code for code, _ in evidence if code in DYNAMIC_EVIDENCE]
            if dynamic and len(evidence) >= cfg.approach_min_evidence:
                strength = min(
                    100,
                    round(
                        20 * len(evidence)
                        + 30 * (1 - distance / cfg.approach_atr)
                        + 5 * min(zone.touches, 4)
                    ),
                )
                events.append(
                    self._late(
                        EarlyEvent(
                            symbol=symbol,
                            event_type=EventType.BREAKOUT_APPROACH,
                            direction=direction,
                            price=price,
                            detected_at=now,
                            source_timeframe=zone.timeframe,
                            zone=zone,
                            strength=strength,
                            reasons=[code for code, _ in evidence],
                            confirmations=[text for _, text in evidence],
                            metrics={
                                **base,
                                "distance_to_level_atr": round(distance, 3),
                            },
                            # one per symbol + direction and distance band, so a
                            # close approach is its own record after a wide one
                            key=APPROACH_KEY
                            + (
                                ":close"
                                if distance <= cfg.approach_notify_atr
                                else ":wide"
                            ),
                        ),
                        ctx,
                    )
                )
        # ZONE_WATCH: price arrives INTO an important 1H/4H zone (seen live).
        if self.zone_watch_enabled and zone.htf and zone.source != "PATTERN":
            width = cfg.zone_watch_atr * ref
            inside = zone.lower - width <= price <= zone.upper + width
            context_direction = "LONG" if zone.kind == "SUPPORT" else "SHORT"
            if inside and arrived_live:
                events.append(
                    EarlyEvent(
                        symbol=symbol,
                        event_type=EventType.ZONE_WATCH,
                        direction=context_direction,
                        price=price,
                        detected_at=now,
                        source_timeframe=zone.timeframe,
                        zone=zone,
                        reasons=[f"{zone.timeframe.upper()}_{zone.kind}_{zone.source}"],
                        metrics={
                            **base,
                            "distance_to_zone_atr": round(
                                max(zone.lower - price, price - zone.upper, 0.0) / ref,
                                3,
                            ),
                            "distance_to_zone_pct": round(
                                max(zone.lower - price, price - zone.upper, 0.0)
                                / price
                                * 100,
                                3,
                            ),
                            "zone_age_hours": _age_hours(zone, now),
                        },
                        key=zone.key,
                    )
                )
        return events

    def _approach_evidence(
        self,
        ctx: StructureContext,
        tracker: SymbolTracker,
        zone: KeyZone,
        direction: str,
        price: float,
        now: float,
    ) -> list[tuple[str, str]]:
        evidence = []
        if any(
            p.direction == direction
            and p.pattern_type
            in ("ASCENDING_TRIANGLE", "DESCENDING_TRIANGLE", "BREAKOUT_COMPRESSION")
            for p in ctx.patterns
        ):
            evidence.append(("COMPRESSION", "структура сжимается"))
        ratio = tracker.volume_ratio()
        if ratio is not None and ratio >= self.cfg.volume_confirm:
            evidence.append(("VOLUME_RISING", f"объём растёт (×{ratio:.1f} к норме)"))
        if zone.touches >= 3:
            evidence.append(
                ("REPEATED_TESTS", f"уровень тестировался {zone.touches} раз")
            )
        earlier = tracker.price_at(now - 300)
        sign = 1 if direction == "LONG" else -1
        if earlier and sign * (price - earlier) >= 0.25 * zone.atr:
            evidence.append(("MOMENTUM_TO_LEVEL", "цена движется к уровню"))
        if ctx.oi_change_15m is not None and ctx.oi_change_15m >= 0.5:
            evidence.append(
                ("OI_EXPANSION", f"OI растёт ({ctx.oi_change_15m:+.2f}% за 15m)")
            )
        return evidence

    def _progress(
        self,
        symbol: str,
        ctx: StructureContext,
        tracker: SymbolTracker,
        price: float,
        now: float,
    ) -> list[EarlyEvent]:
        cfg = self.cfg
        events: list[EarlyEvent] = []
        for key, ep in list(self.episodes[symbol].items()):
            ref = ep.zone.atr
            terminal = ep.phase in ("CONFIRMED", "FAILED")
            expired = now - ep.break_time > cfg.episode_max_hours * 3600 or (
                ep.phase == "RETESTING"
                and ep.retest_started is not None
                and now - ep.retest_started > cfg.retest_max_minutes * 60
            )
            if expired or (terminal and now - ep.updated > 1800):
                ep.advance("COOLED_DOWN", now)
                self.finished.append(ep)
                del self.episodes[symbol][key]
                continue
            if terminal or ref <= 0:
                continue
            sign = ep.sign
            beyond = sign * (price - ep.level)
            ep.max_extension = max(ep.max_extension, beyond)
            base = {
                "zone_timeframe": ep.zone.timeframe,
                "velocity_1m_pct": _change(tracker, now, 60, price),
                "volume_ratio": _round(tracker.volume_ratio()),
                "oi_change_15m_pct": ctx.oi_change_15m,
                "max_extension_atr": round(ep.max_extension / ref, 3),
                "move_since_break_pct": round(
                    sign * (price / ep.break_price - 1) * 100, 3
                ),
                "minutes_since_break": round((now - ep.break_time) / 60, 1),
            }
            fail_line = ep.level - sign * cfg.fail_atr * ref
            count, start = _streak(
                tracker, lambda p, s=sign, f=fail_line: s * (p - f) < 0
            )
            if (
                count >= cfg.break_min_samples
                and start is not None
                and now - start >= cfg.break_hold_seconds
            ):
                ep.advance("FAILED", now)
                events.append(
                    self._episode_event(
                        ep,
                        EventType.FAILED_BREAKOUT,
                        price,
                        now,
                        ["LEVEL_LOST", "BACK_INSIDE_RANGE"],
                        [
                            "уровень не удержан",
                            "цена вернулась внутрь предыдущего диапазона",
                        ],
                        {**base, "depth_inside_atr": round(-beyond / ref, 3)},
                    )
                )
                continue
            if ep.phase == "BROKEN":
                momentum = self._momentum(symbol, ep, ctx, tracker, now)
                if momentum and beyond > 0:
                    ep.momentum_sent = True
                    events.append(
                        self._episode_event(
                            ep,
                            EventType.MOMENTUM_CONFIRMED,
                            price,
                            now,
                            [code for code, _ in momentum],
                            [text for _, text in momentum],
                            base,
                            strength=min(100, 30 + 20 * len(momentum)),
                        )
                    )
                if (
                    ep.max_extension >= cfg.retest_departure_atr * ref
                    and beyond <= cfg.retest_atr * ref
                ):
                    ep.retest_started, ep.retest_extreme = now, price
                    ep.advance("RETESTING", now)
                    events.append(
                        self._episode_event(
                            ep,
                            EventType.RETEST_WATCH,
                            price,
                            now,
                            ["RETURN_TO_BROKEN_ZONE"],
                            ["цена вернулась к ранее пробитому уровню"],
                            {**base, "retest_depth_atr": round(-beyond / ref, 3)},
                        )
                    )
            elif ep.phase == "RETESTING":
                assert ep.retest_extreme is not None
                ep.retest_extreme = (
                    min(ep.retest_extreme, price)
                    if sign > 0
                    else max(ep.retest_extreme, price)
                )
                bounce = sign * (price - ep.retest_extreme)
                if bounce >= cfg.retest_confirm_atr * ref and beyond > 0:
                    found = self._retest_confirmations(ep, ctx, tracker)
                    # NO_DEEP_RETURN (first) is required and does not count.
                    if (
                        found is not None
                        and len(found) - 1 >= cfg.retest_min_confirmations
                        and any(code in DYNAMIC_RETEST for code, _ in found)
                    ):
                        ep.advance("CONFIRMED", now)
                        invalidation = ep.retest_extreme - sign * 0.1 * ref
                        events.append(
                            self._episode_event(
                                ep,
                                EventType.RETEST_CONFIRMED,
                                price,
                                now,
                                [code for code, _ in found],
                                [text for _, text in found],
                                {
                                    **base,
                                    "retest_extreme": ep.retest_extreme,
                                    "retest_depth_atr": round(
                                        sign * (ep.level - ep.retest_extreme) / ref, 3
                                    ),
                                    "invalidation": invalidation,
                                    "risk_pct": round(
                                        abs(price - invalidation) / price * 100, 3
                                    ),
                                },
                                strength=min(100, 40 + 15 * len(found)),
                            )
                        )
        return events

    def _episode_event(
        self, ep, event_type, price, now, reasons, confirmations, metrics, strength=None
    ) -> EarlyEvent:
        return EarlyEvent(
            symbol=ep.symbol,
            event_type=event_type,
            direction=ep.direction,
            price=price,
            detected_at=now,
            source_timeframe=ep.zone.timeframe,
            zone=ep.zone,
            strength=strength,
            reasons=reasons,
            confirmations=confirmations,
            metrics={**metrics, "late_episode": ep.late},
            episode=ep,
            key=str(ep.id),
        )

    def _momentum(
        self,
        symbol: str,
        ep: BreakoutEpisode,
        ctx: StructureContext,
        tracker: SymbolTracker,
        now: float,
    ) -> list[tuple[str, str]]:
        if (
            ep.momentum_sent
            or now - self.recent_fast.get((symbol, ep.direction), -1e18) <= 600
        ):
            return []  # a fast CONFIRMED/EXTREME alert already told this story
        found = []
        ratio = tracker.volume_ratio()
        if ratio is not None and ratio >= self.cfg.momentum_volume:
            found.append(("VOLUME_ANOMALY", f"аномальный объём (×{ratio:.1f} к норме)"))
        if any(
            e.direction == ep.direction
            and e.timestamp.timestamp() >= ep.break_time - 300
            for e in ctx.breaks5
        ):
            found.append(("CLEAN_5M_BREAK", "5m BOS в сторону пробоя (закрытая свеча)"))
        if (
            ctx.oi_change_15m is not None
            and ctx.oi_change_15m >= 1.0
            and ctx.built_at.timestamp() >= ep.break_time
        ):
            found.append(
                ("OI_EXPANSION", f"OI расширяется ({ctx.oi_change_15m:+.2f}% за 15m)")
            )
        wanted = "bullish" if ep.direction == "LONG" else "bearish"
        known = [
            v for v in ctx.market.values() if v in ("bullish", "bearish", "neutral")
        ]
        if known and all(v == wanted for v in known):
            found.append(("MARKET_ALIGNED", "BTC/ETH context совпадает по направлению"))
        return found if len(found) >= 2 else []

    def _retest_confirmations(
        self, ep: BreakoutEpisode, ctx: StructureContext, tracker: SymbolTracker
    ) -> list[tuple[str, str]] | None:
        """None when the retest went too deep (required condition); else evidence."""
        cfg, ref, sign = self.cfg, ep.zone.atr, ep.sign
        assert ep.retest_extreme is not None
        if sign * (ep.retest_extreme - ep.level) < -cfg.retest_atr * ref:
            return None
        found = [("NO_DEEP_RETURN", "нет глубокого возврата в прежний диапазон")]
        bars = [
            b
            for b in tracker.bars1m
            if ep.retest_started and b[0] / 1000 >= ep.retest_started - 60
        ]
        if len(bars) >= 2:
            last = bars[-1]
            prior = bars[-4:-1] if len(bars) >= 4 else bars[:-1]
            if sign > 0 and last[4] > max(b[2] for b in prior):
                found.append(("MICRO_BOS_1M", "micro BOS вверх (1m, закрытая минута)"))
            if sign < 0 and last[4] < min(b[3] for b in prior):
                found.append(("MICRO_BOS_1M", "micro BOS вниз (1m, закрытая минута)"))
        base = [b for b in ctx.bars5 if b.close_time.timestamp() <= ep.break_time][-6:]
        if base:
            if sign > 0 and ep.retest_extreme > min(float(b.low) for b in base):
                found.append(
                    ("HIGHER_LOW", "5m higher low относительно базы до пробоя")
                )
            if sign < 0 and ep.retest_extreme < max(float(b.high) for b in base):
                found.append(
                    ("LOWER_HIGH", "5m lower high относительно базы до пробоя")
                )
        ratio = tracker.volume_ratio()
        if ratio is not None and ratio >= cfg.volume_confirm:
            found.append(("VOLUME_RENEWED", f"объём снова растёт (RVOL {ratio:.1f}x)"))
        if ctx.oi_change_15m is not None and ctx.oi_change_15m >= 0:
            found.append(
                ("OI_HOLDING", f"OI держится ({ctx.oi_change_15m:+.2f}% за 15m)")
            )
        against = "bearish" if sign > 0 else "bullish"
        known = [
            v for v in ctx.market.values() if v in ("bullish", "bearish", "neutral")
        ]
        if known and against not in known:
            found.append(("MARKET_COMPATIBLE", "BTC/ETH context совместим"))
        return found

    # ---- restart ---------------------------------------------------------------------

    def restore_episode(self, ep: BreakoutEpisode) -> None:
        self.episodes[ep.symbol][ep.key] = ep


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _age_hours(zone: KeyZone, now: float) -> float | None:
    if zone.created_at is None:
        return None
    return round((now - zone.created_at.timestamp()) / 3600, 1)
