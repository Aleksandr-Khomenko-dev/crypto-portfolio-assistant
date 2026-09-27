"""Fast-move detection (pure). An EARLY-WARNING layer, never a trading score.

Per symbol we keep realtime (receipt-time, price) samples and the running 1m volume,
plus a baseline of CLOSED 1-minute candles (volatility and normal volume). A move
triggers only when it is large relative to that asset's own recent volatility AND an
absolute configurable floor, so ordinary noise on a volatile coin does not alert.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from statistics import median

WINDOWS = {"15s": 15, "30s": 30, "1m": 60, "3m": 180, "5m": 300}


class FastState(StrEnum):
    WATCHING = "WATCHING"
    FAST_MOVE = "FAST_MOVE"
    CONFIRMED_MOMENTUM = "CONFIRMED_MOMENTUM"
    EXTREME_MOVE = "EXTREME_MOVE"
    COOLED_DOWN = "COOLED_DOWN"


RANK = {
    FastState.WATCHING: 0,
    FastState.COOLED_DOWN: 0,
    FastState.FAST_MOVE: 1,
    FastState.CONFIRMED_MOMENTUM: 2,
    FastState.EXTREME_MOVE: 3,
}


@dataclass(frozen=True)
class FastConfig:
    """Research defaults; every value is configurable via settings."""

    # Absolute floors (percent) per window: a move must exceed BOTH the floor and the
    # volatility-scaled threshold.
    min_pct: dict[str, float] = field(
        default_factory=lambda: {
            "15s": 1.0,
            "30s": 1.2,
            "1m": 1.0,
            "3m": 1.8,
            "5m": 2.5,
        }
    )
    extreme_pct: dict[str, float] = field(
        default_factory=lambda: {
            "15s": 1.5,
            "30s": 2.0,
            "1m": 2.5,
            "3m": 4.0,
            "5m": 6.0,
        }
    )
    sigma_k: float = 4.0  # FAST_MOVE: |move| >= k * sigma(window)
    sigma_k_extreme: float = 8.0  # EXTREME_MOVE
    volume_expansion: float = 3.0  # last-60s volume vs median closed minute
    min_baseline_minutes: int = (
        20  # below this the symbol is not armed (no noise alerts)
    )
    min_updates_per_minute: int = 10  # thin/pathological streams are ignored
    extension_ratio: float = 0.5  # re-alert when the move extends by 50% more
    cooldown_seconds: int = 1800  # per symbol+direction, independent of the scanner


@dataclass
class Trigger:
    direction: str  # LONG (up) / SHORT (down): direction of the move, not a trade
    state: FastState
    window: str
    change_pct: float
    threshold_pct: float
    sigma_pct: float
    volume_ratio: float | None
    reasons: list[str]
    price: float


class SymbolTracker:
    """Realtime samples + closed-minute baseline for one symbol."""

    def __init__(self, symbol: str, baseline_minutes: int = 60) -> None:
        self.symbol = symbol
        self.samples: deque[tuple[float, float]] = deque()  # (epoch seconds, price)
        self.volume_steps: deque[tuple[float, float]] = deque()  # (epoch, volume added)
        self.closed: deque[tuple[float, float]] = deque(
            maxlen=baseline_minutes
        )  # (close, volume)
        self.minute_open: int | None = None  # open time (ms) of the running minute
        self.minute_volume = 0.0
        self.minute_close: float | None = None
        self.updates: deque[float] = deque()
        self.first_stream_minute: int | None = None  # open ms of first streamed minute
        # Closed 1m bars seen live as (open_ms, open, high, low, close): micro
        # confirmation only (early engine), never a closed-candle scanner input.
        self.bars1m: deque[tuple[int, float, float, float, float]] = deque(maxlen=30)
        self.minute_ohl: tuple[float, float, float] | None = None

    def seed(self, bars: list[tuple[int, float, float]]) -> None:
        """Baseline from CLOSED 1m candles (REST) as (open_ms, close, volume), oldest
        first. Only minutes before the first streamed minute are used, and they are
        PREPENDED: minutes already closed by the live stream are kept, never skipped."""
        cutoff = self.first_stream_minute
        history = [
            (c, v) for open_ms, c, v in bars if cutoff is None or open_ms < cutoff
        ]
        merged = [*history, *self.closed]
        self.closed.clear()
        self.closed.extend(merged[-(self.closed.maxlen or len(merged)) :])

    def on_kline(
        self,
        open_ms: int,
        close: float,
        volume: float,
        received: float,
        high: float | None = None,
        low: float | None = None,
        open_: float | None = None,
    ) -> None:
        if self.first_stream_minute is None:
            self.first_stream_minute = open_ms
        if self.minute_open is not None and open_ms > self.minute_open:
            # The previous minute closed: it joins the baseline (closed data only).
            if self.minute_close is not None:
                self.closed.append((self.minute_close, self.minute_volume))
                if self.minute_ohl is not None:
                    self.bars1m.append(
                        (self.minute_open, *self.minute_ohl, self.minute_close)
                    )
            self.minute_volume = 0.0
            self.minute_ohl = None
        if self.minute_open is None or open_ms >= self.minute_open:
            added = max(0.0, volume - self.minute_volume)
            if added:
                self.volume_steps.append((received, added))
            o, h, l_ = self.minute_ohl or (open_ or close, close, close)
            self.minute_ohl = (
                o,
                max(h, high if high is not None else close),
                min(l_, low if low is not None else close),
            )
            self.minute_open, self.minute_volume, self.minute_close = (
                open_ms,
                volume,
                close,
            )
        self.samples.append((received, close))
        self.updates.append(received)
        horizon = received - max(WINDOWS.values()) - 5
        while self.samples and self.samples[0][0] < horizon:
            self.samples.popleft()
        while self.volume_steps and self.volume_steps[0][0] < received - 60:
            self.volume_steps.popleft()
        while self.updates and self.updates[0] < received - 60:
            self.updates.popleft()

    def price_at(self, when: float) -> float | None:
        """Latest price observed at or before `when` (None if history is too short)."""
        if not self.samples or self.samples[0][0] > when:
            return None
        chosen = None
        for at, price in self.samples:
            if at > when:
                break
            chosen = price
        return chosen

    def sigma_1m_pct(self) -> float | None:
        closes = [c for c, _ in self.closed]
        if len(closes) < 2:
            return None
        returns = [math.log(b / a) for a, b in pairwise(closes) if a > 0 and b > 0]
        if not returns:
            return None
        return math.sqrt(sum(r * r for r in returns) / len(returns)) * 100

    def volume_ratio(self) -> float | None:
        baseline = [v for _, v in self.closed if v > 0]
        if len(baseline) < 10:
            return None
        normal = median(baseline)
        return sum(v for _, v in self.volume_steps) / normal if normal > 0 else None


def evaluate(tracker: SymbolTracker, now: float, config: FastConfig) -> Trigger | None:
    """Strongest qualifying move across windows, or None. Pure and deterministic."""
    if len(tracker.closed) < config.min_baseline_minutes:
        return None  # not armed: no reliable volatility baseline yet
    if len(tracker.updates) < config.min_updates_per_minute:
        return None  # stream too thin to trust short windows
    sigma = tracker.sigma_1m_pct()
    last = tracker.samples[-1][1] if tracker.samples else None
    if sigma is None or last is None:
        return None
    volume = tracker.volume_ratio()
    best: Trigger | None = None
    for name, seconds in WINDOWS.items():
        start = tracker.price_at(now - seconds)
        if not start:
            continue
        change = (last / start - 1) * 100
        sigma_w = sigma * math.sqrt(seconds / 60)
        threshold = max(config.min_pct[name], config.sigma_k * sigma_w)
        if abs(change) < threshold:
            continue
        extreme = max(config.extreme_pct[name], config.sigma_k_extreme * sigma_w)
        expanding = volume is not None and volume >= config.volume_expansion
        # Sub-minute flicks without volume are ordinary noise (observed live: +-0.6%
        # 15 s whipsaws); they count only with volume confirmation or at extreme size.
        if seconds < 60 and not expanding and abs(change) < extreme:
            continue
        reasons = [f"PRICE_ACCELERATION_{name.upper()}"]
        state = FastState.FAST_MOVE
        if expanding:
            reasons.append("VOLUME_EXPANSION")
            state = FastState.CONFIRMED_MOMENTUM
        if abs(change) >= extreme:
            state = FastState.EXTREME_MOVE
        candidate = Trigger(
            direction="LONG" if change > 0 else "SHORT",
            state=state,
            window=name,
            change_pct=round(change, 3),
            threshold_pct=round(threshold, 3),
            sigma_pct=round(sigma_w, 4),
            volume_ratio=round(volume, 2) if volume is not None else None,
            reasons=reasons,
            price=last,
        )
        strength = (RANK[state], abs(change) / threshold)
        if best is None or strength > (
            RANK[best.state],
            abs(best.change_pct) / best.threshold_pct,
        ):
            best = candidate
    return best


@dataclass
class EpisodeMemory:
    state: FastState
    alerted_change: float
    last_seen: float
    reasons: set[str]


class AntiSpam:
    """Per symbol+direction: first alert once; re-alert only on upgrade, material
    extension, a new confirmation, or a genuinely new event after the cooldown."""

    def __init__(self, config: FastConfig) -> None:
        self.config = config
        self.episodes: dict[tuple[str, str], EpisodeMemory] = {}

    def decide(self, symbol: str, trigger: Trigger, now: float) -> str | None:
        key = (symbol, trigger.direction)
        episode = self.episodes.get(key)
        if episode is None or now - episode.last_seen > self.config.cooldown_seconds:
            self.episodes[key] = EpisodeMemory(
                trigger.state, trigger.change_pct, now, set(trigger.reasons)
            )
            return "NEW"
        episode.last_seen = now
        new_reasons = set(trigger.reasons) - episode.reasons
        if RANK[trigger.state] > RANK[episode.state]:
            episode.state, episode.alerted_change = trigger.state, trigger.change_pct
            episode.reasons |= new_reasons
            return "UPGRADE"
        extended = abs(trigger.change_pct) >= abs(episode.alerted_change) * (
            1 + self.config.extension_ratio
        )
        if extended:
            episode.alerted_change = trigger.change_pct
            episode.reasons |= new_reasons
            return "EXTENSION"
        if new_reasons - {r for r in new_reasons if r.startswith("PRICE_ACCELERATION")}:
            episode.reasons |= new_reasons
            return "CONFIRMATION"
        return None

    def seed(
        self, symbol: str, direction: str, state: FastState, change: float, at: datetime
    ) -> None:
        """Restore recent episodes after a restart so the cooldown survives it."""
        self.episodes[(symbol, direction)] = EpisodeMemory(
            state, change, at.timestamp(), set()
        )
