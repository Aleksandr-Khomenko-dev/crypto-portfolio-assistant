"""News latency telemetry and per-provider SLO status.

Internal latency (receipt -> Telegram) is measured by us. Upstream latency (source
publication -> our receipt) is reported separately and depends on the provider.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.news.domain import Transport

LATENCY_KEYS = (
    "news_ingest_latency_ms",  # received -> normalized/accepted
    "news_classification_latency_ms",  # accepted -> classified
    "news_setup_link_latency_ms",  # classified -> linked
    "news_telegram_latency_ms",  # dispatch start -> Telegram accepted
    "news_total_latency_ms",  # received -> Telegram accepted
    "news_upstream_latency_ms",  # source published -> received (provider-bound)
)


class Samples:
    def __init__(self, size: int = 2000) -> None:
        self.values: deque[float] = deque(maxlen=size)

    def add(self, value: float) -> None:
        self.values.append(value)

    def percentile(self, q: float) -> float | None:
        if not self.values:
            return None
        ordered = sorted(self.values)
        index = min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))
        return round(ordered[index], 1)

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": len(self.values),
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
        }


@dataclass
class ProviderHealth:
    name: str
    transport: Transport
    interval_seconds: float | None  # polling cadence; None for push transports
    stale_after_seconds: float
    last_success_mono: float | None = None
    last_success_at: datetime | None = None
    last_attempt_mono: float | None = None
    errors: int = 0
    reconnects: int = 0
    stale_events: int = 0
    poll_delay_seconds: float | None = None  # actual gap between polls
    extra: dict[str, float] = field(default_factory=dict)

    def success(self) -> None:
        now = time.monotonic()
        if self.last_attempt_mono is not None and self.interval_seconds:
            self.poll_delay_seconds = round(now - self.last_attempt_mono, 3)
        self.last_success_mono = self.last_attempt_mono = now
        self.last_success_at = datetime.now(UTC)

    def attempt(self) -> None:
        self.last_attempt_mono = time.monotonic()

    def age(self) -> float | None:
        if self.last_success_mono is None:
            return None
        return time.monotonic() - self.last_success_mono

    def is_stale(self) -> bool:
        age = self.age()
        return age is None or age > self.stale_after_seconds


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class NewsMetrics:
    def __init__(self, slo_ms: int = 3000) -> None:
        self.slo_ms = slo_ms
        self.counters: Counter[str] = Counter()
        self.latency = {key: Samples() for key in LATENCY_KEYS}
        self.providers: dict[str, ProviderHealth] = {}
        self.delayed_over_slo = 0

    def record(self, key: str, value_ms: float) -> None:
        self.latency[key].add(value_ms)
        if key == "news_total_latency_ms" and value_ms > self.slo_ms:
            self.delayed_over_slo += 1

    def status(self, provider: ProviderHealth) -> str:
        if provider.is_stale():
            return "STALE"
        p95 = self.latency["news_total_latency_ms"].percentile(95)
        return "DEGRADED" if p95 is not None and p95 > self.slo_ms else "REALTIME_OK"

    def snapshot(self, queue_depth: int = 0) -> dict:
        return {
            "counters": dict(self.counters),
            "latency": {key: s.summary() for key, s in self.latency.items()},
            "events_delayed_over_slo": self.delayed_over_slo,
            "slo_ms": self.slo_ms,
            "news_queue_depth": queue_depth,
            "providers": {
                name: {
                    "transport": h.transport.value,
                    "poll_interval_seconds": h.interval_seconds,
                    "poll_delay_seconds": h.poll_delay_seconds,
                    "last_success_at": h.last_success_at.isoformat()
                    if h.last_success_at
                    else None,
                    "age_seconds": _rounded(h.age()),
                    "errors": h.errors,
                    "reconnects": h.reconnects,
                    "stale_events": h.stale_events,
                    "slo_status": self.status(h),
                }
                for name, h in self.providers.items()
            },
        }
