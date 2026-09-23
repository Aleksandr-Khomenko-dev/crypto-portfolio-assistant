"""Group completed setup outcomes by score band. Pure aggregation; no scoring changes."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import mean, median

from app.research.outcomes import (
    AMBIGUOUS,
    DATA_GAP,
    EXPIRED,
    INVALIDATION_FIRST,
    KEYS,
    TARGET_FIRST,
    TARGET_LABELS,
    TERMINAL_STATUSES,
    UNRESOLVED,
)
from app.research.schemas import BandStats, CalibrationGroup, TargetRate

MAJORS = ("BTCUSDT", "ETHUSDT")
GROUP_FIELDS = (
    "exchange",
    "symbol",
    "direction",
    "timeframe",
    "market_regime",
    "structure_regime",
    "asset_class",
)


@dataclass(frozen=True)
class OutcomeRecord:
    symbol: str
    direction: str
    timeframe: str
    market_regime: str
    structure_regime: str
    score: int
    status: str
    first: dict[str, str]
    mfe_r: float
    mae_r: float
    exchange: str = "BINANCE"

    @property
    def asset_class(self) -> str:
        return "MAJOR" if self.symbol in MAJORS else "ALT"


def bands(edges: Sequence[int]) -> list[tuple[int, int]]:
    """Edges (60, 70, 80, 90) -> 60–69, 70–79, 80–89, 90–100, plus 0–59 below."""
    ranges = [(0, edges[0] - 1)] if edges[0] > 0 else []
    ranges += [(low, high - 1) for low, high in pairwise(edges)]
    return [*ranges, (edges[-1], 100)]


def band_stats(
    low: int, high: int, records: list[OutcomeRecord], min_samples: int
) -> BandStats:
    finished = [r for r in records if r.status in TERMINAL_STATUSES]
    sample = [r for r in finished if r.status != DATA_GAP]
    targets = []
    for key in KEYS:
        values = [r.first[key] for r in sample]
        hit, miss = values.count(TARGET_FIRST), values.count(INVALIDATION_FIRST)
        targets.append(
            TargetRate(
                target=TARGET_LABELS[key],
                target_first=hit,
                invalidation_first=miss,
                resolved=hit + miss,
                ambiguous=values.count(AMBIGUOUS),
                unresolved=values.count(UNRESOLVED),
                observed_rate=hit / (hit + miss) if hit + miss else None,
            )
        )
    mfe = [r.mfe_r for r in sample]
    mae = [r.mae_r for r in sample]
    return BandStats(
        band=f"{low}–{high}",
        score_min=low,
        score_max=high,
        sample_count=len(sample),
        low_sample=len(sample) < min_samples,
        targets=targets,
        avg_mfe_r=mean(mfe) if mfe else None,
        median_mfe_r=median(mfe) if mfe else None,
        avg_mae_r=mean(mae) if mae else None,
        median_mae_r=median(mae) if mae else None,
        ambiguous_count=sum(AMBIGUOUS in r.first.values() for r in sample),
        expired_count=sum(r.status == EXPIRED for r in sample),
        data_gap_count=len(finished) - len(sample),
    )


def calibrate(
    records: Iterable[OutcomeRecord],
    edges: Sequence[int],
    group_by: Sequence[str] = (),
    min_samples: int = 30,
) -> list[CalibrationGroup]:
    unknown = set(group_by) - set(GROUP_FIELDS)
    if unknown:
        raise ValueError("Unsupported group_by: " + ", ".join(sorted(unknown)))
    grouped: dict[tuple[str, ...], list[OutcomeRecord]] = {}
    for record in records:
        key = tuple(str(getattr(record, name)) for name in group_by)
        grouped.setdefault(key, []).append(record)
    result = []
    for key, members in sorted(grouped.items()):
        stats = [
            band_stats(
                low,
                high,
                [r for r in members if low <= r.score <= high],
                min_samples,
            )
            for low, high in bands(edges)
        ]
        # The below-threshold band is shown only when it has observations.
        stats = [
            s
            for s in stats
            if s.score_min >= edges[0] or s.sample_count or s.data_gap_count
        ]
        result.append(
            CalibrationGroup(
                key=dict(zip(group_by, key)),
                sample_count=sum(s.sample_count for s in stats),
                bands=stats,
            )
        )
    return result
