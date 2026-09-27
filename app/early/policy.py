"""Telegram notification policy (pure). DETECTION is separate: every valid event is
persisted and enters outcome research whether or not it is sent.

The policy only decides whether the user is told, groups related events into one
user-facing structural episode (symbol + direction + level), threads follow-ups as
replies, and explains every suppression with one reason:

* ``policy``    - research-only type, or not qualified for the user (yet);
* ``duplicate`` - the same user-facing episode already carried this stage;
* ``late``      - the move was already extended (never sold as "early");
* ``rate_limit`` - pre-event alerts (ignition/approach) share a smoothing budget.

FIRST_BREAK, RETEST_WATCH and RETEST_CONFIRMED are never rate limited.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

from app.early.model import EarlyEvent, EventType, KeyZone

# Stage within one structural episode: a lower or equal stage at the same level is
# redundant once a higher one has been shown.
STAGE = {
    EventType.BULLISH_IGNITION: 1,
    EventType.BEARISH_IGNITION: 1,
    EventType.BREAKOUT_APPROACH: 2,
    EventType.FIRST_BREAK: 3,
}
RESEARCH_ONLY = {EventType.ZONE_WATCH, EventType.FORMATION_WATCH}
BUDGETED = {
    EventType.BULLISH_IGNITION,
    EventType.BEARISH_IGNITION,
    EventType.BREAKOUT_APPROACH,
}
IGNITION_CONFIRMATIONS = {"VOLUME_EXPANSION", "OI_EXPANSION", "OI_STABLE"}
IGNITION_SHIFTS = {"CHOCH_5M", "BOS_5M", "ZONE_RECLAIM", "LIQUIDITY_SWEEP"}
APPROACH_SUPPORT = {
    "COMPRESSION",
    "MOMENTUM_TO_LEVEL",
    "VOLUME_RISING",
    "OI_EXPANSION",
    "REPEATED_TESTS",
}
STATS = (
    "detected",
    "persisted",
    "telegram_eligible",
    "sent",
    "suppressed_by_policy",
    "suppressed_duplicate",
    "suppressed_late",
    "suppressed_rate_limit",
)


@dataclass(frozen=True)
class PolicyConfig:
    approach_notify_atr: float = 0.20  # Telegram only this close (detection: 0.35)
    episode_merge_atr: float = 0.5  # same user-facing level within this distance
    thread_hours: float = 6.0  # follow-ups reply under the episode's first message
    budget_per_5min: int = 3  # ignition/approach smoothing, market-wide
    budget_per_hour: int = 12
    fast_near_htf_atr: float = 0.5  # FAST_MOVE counts as "at structure" within this
    represented_seconds: float = 900.0  # EXTREME already told by a break/retest
    same_moment_seconds: float = 120.0  # a fast move right after any alert = same story
    notify_zone_watch: bool = False  # research/context by default
    notify_formation: bool = False  # research/context by default


@dataclass
class Decision:
    send: bool
    reason: str  # "eligible" or the suppression reason
    detail: str = ""
    reply_to: int | None = None
    episode_key: str | None = None


@dataclass
class UserEpisode:
    key: str
    symbol: str
    direction: str
    level: float | None
    atr: float
    first_message_id: int | None = None
    stages: dict[str, float] = field(default_factory=dict)  # event type -> last sent
    updated: float = 0.0


def zone_level(zone: KeyZone | None) -> tuple[float | None, float]:
    if zone is None:
        return None, 0.0
    return (zone.lower + zone.upper) / 2, zone.atr


class NotificationPolicy:
    def __init__(self, cfg: PolicyConfig | None = None) -> None:
        self.cfg = cfg or PolicyConfig()
        self.episodes: dict[tuple[str, str], list[UserEpisode]] = defaultdict(list)
        self.visible_breakouts: set[str] = set()  # breakout episode ids shown
        self.hidden_breakouts: set[str] = set()  # overlapping duplicates, never shown
        self.budget: deque[float] = deque()
        self.stats: dict[str, Counter[str]] = defaultdict(Counter)

    # ---- bookkeeping ------------------------------------------------------------

    def count(self, event_type: str, stat: str, n: int = 1) -> None:
        self.stats[event_type][stat] += n

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {
            kind: {stat: counts.get(stat, 0) for stat in STATS}
            for kind, counts in sorted(self.stats.items())
        }

    def _find(
        self, symbol: str, direction: str, level: float | None, atr: float, now: float
    ) -> tuple[UserEpisode | None, UserEpisode | None]:
        """(episode at the same level, latest episode for symbol+direction) - both
        only within the threading window."""
        same = latest = None
        for episode in self.episodes.get((symbol, direction), []):
            if now - episode.updated > self.cfg.thread_hours * 3600:
                continue
            if latest is None or episode.updated > latest.updated:
                latest = episode
            reference = max(atr, episode.atr)
            if (
                level is not None
                and episode.level is not None
                and reference > 0
                and abs(level - episode.level) <= self.cfg.episode_merge_atr * reference
            ):
                same = episode
        return same, latest

    def _budget_ok(self, now: float) -> bool:
        while self.budget and now - self.budget[0] > 3600:
            self.budget.popleft()
        recent = sum(now - at <= 300 for at in self.budget)
        return (
            len(self.budget) < self.cfg.budget_per_hour
            and recent < self.cfg.budget_per_5min
        )

    # ---- decisions ------------------------------------------------------------------

    def _qualifies(self, event: EarlyEvent) -> tuple[bool, str]:
        t, reasons = event.event_type, set(event.reasons)
        if t == EventType.ZONE_WATCH and not self.cfg.notify_zone_watch:
            return False, "zone watch is research/context only"
        if t == EventType.FORMATION_WATCH and not self.cfg.notify_formation:
            return False, "formation is research/context only"
        if t in (EventType.BULLISH_IGNITION, EventType.BEARISH_IGNITION):
            if event.zone is None or not event.zone.htf:
                return False, "ignition not at a 1H/4H zone"
            if not reasons & IGNITION_SHIFTS:
                return False, "no recent structure shift"
            strong_reclaim = {"LIQUIDITY_SWEEP", "ZONE_RECLAIM"} <= reasons
            if not (reasons & IGNITION_CONFIRMATIONS or strong_reclaim):
                return False, "no volume/OI/strong-reclaim confirmation"
            return True, ""
        if t == EventType.BREAKOUT_APPROACH:
            distance = event.metrics.get("distance_to_level_atr")
            if distance is None or distance > self.cfg.approach_notify_atr:
                return False, f"approach wider than {self.cfg.approach_notify_atr} ATR"
            if not reasons & APPROACH_SUPPORT:
                return False, "no supporting condition"
            return True, ""
        if t == EventType.FAILED_BREAKOUT:
            episode = str(event.episode.id) if event.episode else ""
            if episode not in self.visible_breakouts:
                return False, "user never saw this breakout"
            return True, ""
        return True, ""

    def decide_early(
        self, event: EarlyEvent, now: float, setup_reply: int | None = None
    ) -> Decision:
        t = event.event_type
        if t == EventType.LATE_EXTENDED_MOVE:
            return Decision(False, "late", "move already extended")
        ok, why = self._qualifies(event)
        if not ok:
            return Decision(False, "policy", why)
        level, atr = zone_level(event.zone)
        same, latest = self._find(event.symbol, event.direction, level, atr, now)
        breakout = str(event.episode.id) if event.episode else None
        if breakout is not None and breakout in self.hidden_breakouts:
            return Decision(False, "duplicate", "overlapping breakout of a shown level")
        if t in STAGE and same is not None:
            shown = [STAGE[EventType(k)] for k in same.stages if EventType(k) in STAGE]
            if shown and max(shown) >= STAGE[t]:
                if t == EventType.FIRST_BREAK and breakout is not None:
                    self.hidden_breakouts.add(breakout)
                return Decision(False, "duplicate", "episode already at this stage")
        if (
            t == EventType.MOMENTUM_CONFIRMED
            and same is not None
            and t.value in same.stages
        ):
            return Decision(False, "duplicate", "momentum already shown")
        if t in BUDGETED and not self._budget_ok(now):
            return Decision(False, "rate_limit", "pre-event smoothing budget")
        anchor = same or latest
        reply = setup_reply or (anchor.first_message_id if anchor else None)
        key = (
            anchor.key
            if anchor is not None and same is not None
            else f"{event.symbol}:{event.direction}:{level}"
        )
        return Decision(True, "eligible", reply_to=reply, episode_key=key)

    def decide_fast(
        self,
        symbol: str,
        direction: str,
        state: str,
        decision: str,
        reasons: list[str],
        now: float,
        *,
        active_setup: bool,
        near_htf: bool,
        setup_reply: int | None = None,
    ) -> Decision:
        """Fast moves are persisted always; told only when they mean something."""
        _, latest = self._find(symbol, direction, None, 0.0, now)
        reply = setup_reply or (latest.first_message_id if latest else None)
        if decision in ("EXTENSION", "CONFIRMATION"):
            return Decision(False, "duplicate", "fast move already told")
        represented = latest is not None and (
            any(
                k in latest.stages
                and now - latest.stages[k] <= self.cfg.represented_seconds
                for k in ("FIRST_BREAK", "RETEST_WATCH", "RETEST_CONFIRMED")
            )
            # any alert for this symbol + direction moments ago already told it
            or any(
                now - at <= self.cfg.same_moment_seconds
                for at in latest.stages.values()
            )
        )
        if represented and state != "CONFIRMED_MOMENTUM":
            # The stronger structural event already told this story (FAST_MOVE and
            # EXTREME_MOVE "notify via the stronger event", never as an extra alert).
            return Decision(False, "duplicate", "represented by an alert already sent")
        if state == "EXTREME_MOVE":
            return Decision(True, "eligible", "extreme move", reply_to=reply)
        if state == "CONFIRMED_MOMENTUM":
            return Decision(True, "eligible", "momentum confirmed", reply_to=reply)
        if active_setup:
            return Decision(True, "eligible", "active setup", reply_to=reply)
        if near_htf:
            return Decision(True, "eligible", "at 1H/4H structure", reply_to=reply)
        if "BREAKOUT_ACCELERATION" in reasons:
            return Decision(True, "eligible", "with a fresh break", reply_to=reply)
        return Decision(False, "policy", "standalone fast move")

    def record_sent(
        self,
        event: EarlyEvent | None,
        symbol: str,
        direction: str,
        event_type: str,
        message_id: int | None,
        now: float,
        episode_key: str | None = None,
    ) -> None:
        """Remember what the user has seen (for threading, dedupe, FAILED rule)."""
        if event is not None and event.episode is not None:
            self.visible_breakouts.add(str(event.episode.id))
        if event_type in RESEARCH_ONLY:
            return
        level, atr = zone_level(event.zone if event else None)
        same, latest = self._find(symbol, direction, level, atr, now)
        episode = same if level is not None else latest
        if episode is None:
            episode = UserEpisode(
                episode_key or f"{symbol}:{direction}:{level}",
                symbol,
                direction,
                level,
                atr,
                first_message_id=message_id,
            )
            self.episodes[(symbol, direction)].append(episode)
        if episode.first_message_id is None:
            episode.first_message_id = message_id
        episode.stages[event_type] = now
        episode.updated = now
        if event_type in BUDGETED:
            self.budget.append(now)

    def restore(
        self,
        symbol: str,
        direction: str,
        event_type: str,
        zone: KeyZone | None,
        breakout_id: str | None,
        message_id: int | None,
        at: float,
    ) -> None:
        """Rebuild what the user has seen from persisted SENT rows after a restart."""
        if breakout_id is not None:
            self.visible_breakouts.add(breakout_id)
        level, atr = zone_level(zone)
        same, latest = self._find(symbol, direction, level, atr, at)
        episode = same if level is not None else latest
        if episode is None:
            episode = UserEpisode(
                f"{symbol}:{direction}:{level}",
                symbol,
                direction,
                level,
                atr,
                message_id,
            )
            self.episodes[(symbol, direction)].append(episode)
        episode.stages[event_type] = at
        episode.updated = max(episode.updated, at)
