"""Closed-time alignment and horizon deltas for self-recorded open interest. Pure.

A live OI observation is attributed to a 15m candle boundary only if it was observed
within `tolerance` seconds of that boundary; otherwise it is not stored at all. No
value is ever interpolated or back-filled, so missing buckets mean "unavailable".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.scanner.domain import Derivatives, OIPoint

BUCKET_SECONDS = 900  # 15m candle boundaries
HORIZONS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


def aligned_bucket(observed_at: datetime, tolerance_seconds: float) -> datetime | None:
    """Nearest 15m boundary, if the observation lies within the tolerance of it."""
    seconds = observed_at.timestamp()
    boundary = round(seconds / BUCKET_SECONDS) * BUCKET_SECONDS
    if abs(seconds - boundary) > tolerance_seconds:
        return None
    return datetime.fromtimestamp(boundary, UTC)


def horizon_changes(
    points: dict[datetime, Decimal], latest_boundary: datetime
) -> dict[str, float | None]:
    """Percent OI change ending at the latest closed boundary, per horizon.

    Both endpoints must be real stored points; otherwise the horizon is None (never 0).
    """
    end = points.get(latest_boundary)
    changes: dict[str, float | None] = {}
    for name, span in HORIZONS.items():
        start = points.get(latest_boundary - span)
        changes[name] = (
            float((end / start - 1) * 100) if end is not None and start else None
        )
    return changes


@dataclass(frozen=True)
class OIObservation:
    bucket_at: datetime
    observed_at: datetime
    open_interest: Decimal  # base quantity
    open_interest_notional: Decimal | None = None

    @property
    def offset(self) -> float:
        return abs((self.observed_at - self.bucket_at).total_seconds())


def observation_from(
    raw: Derivatives, tolerance_seconds: float
) -> OIObservation | None:
    """A storable observation, or None if OI is missing or not near a boundary."""
    if raw.open_interest is None or raw.open_interest <= 0 or raw.oi_timestamp is None:
        return None
    bucket = aligned_bucket(raw.oi_timestamp, tolerance_seconds)
    if bucket is None:
        return None
    return OIObservation(
        bucket, raw.oi_timestamp, raw.open_interest, raw.open_interest_notional
    )


def replaces(existing: OIObservation | None, new: OIObservation) -> bool:
    """Deterministic winner per bucket: the observation closest to the boundary."""
    return existing is None or new.offset < existing.offset


def history_points(
    stored: dict[datetime, OIObservation],
    new: OIObservation | None = None,
    *,
    as_of: datetime | None = None,
) -> list[OIPoint]:
    merged = dict(stored)
    if new is not None and replaces(merged.get(new.bucket_at), new):
        merged[new.bucket_at] = new
    return [
        OIPoint(
            timestamp=bucket, contracts=obs.open_interest, observed_at=obs.observed_at
        )
        for bucket, obs in sorted(merged.items())
        if as_of is None or (bucket <= as_of and obs.observed_at <= as_of)
    ]


def closed_boundary(now: datetime) -> datetime:
    """Current wall-clock 15m boundary; never rounds into the future."""
    return datetime.fromtimestamp(
        int(now.timestamp() // BUCKET_SECONDS) * BUCKET_SECONDS, UTC
    )


def next_boundary(now: datetime) -> datetime:
    """Strictly next boundary, including after a restart exactly on a boundary."""
    return closed_boundary(now) + timedelta(seconds=BUCKET_SECONDS)


def coverage(
    history: dict[str, dict[datetime, OIObservation]],
    symbols: list[str],
    boundary: datetime,
    as_of: datetime,
) -> dict[str, dict[str, int | float]]:
    """Diagnostic exact-endpoint availability, denominator = entire requested universe."""
    counts = dict.fromkeys(HORIZONS, 0)
    for symbol in symbols:
        points = {
            p.timestamp: p.contracts
            for p in history_points(history.get(symbol, {}), as_of=as_of)
        }
        for name, change in horizon_changes(points, boundary).items():
            counts[name] += change is not None
    return {
        name: {
            "available": count,
            "requested": len(symbols),
            "availability_rate": count / len(symbols) if symbols else 0.0,
        }
        for name, count in counts.items()
    }
