from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from app.analytics.levels import fair_value_gaps, zones
from app.analytics.structure import structure
from app.analytics.technical import technical
from app.config import Settings
from app.scanner.domain import INTERVAL_SECONDS, Candle, FrameAnalysis, Timeframe

logger = logging.getLogger(__name__)


def analyze_frame(
    bars: list[Candle],
    timeframe: Timeframe,
    now: datetime,
    settings: Settings,
    previous: FrameAnalysis | None = None,
) -> FrameAnalysis:
    interval = INTERVAL_SECONDS[timeframe]
    cutoff = now - timedelta(seconds=settings.scanner_data_grace_seconds)
    closed = sorted(
        [b for b in bars if b.open_time + timedelta(seconds=interval) <= cutoff],
        key=lambda b: b.open_time,
    )
    for bar in closed:
        duration = (bar.close_time - bar.open_time).total_seconds()
        if (
            bar.open_time.timestamp() % interval
            or not interval - 0.001 <= duration <= interval
        ):
            raise ValueError("Malformed candle interval")
    if len(closed) < 210:
        raise ValueError("Insufficient closed candle history")
    # Closed bars end 1 ms before the boundary; allow that without accepting stale data.
    if (
        now - closed[-1].close_time
    ).total_seconds() > interval + settings.scanner_data_grace_seconds + 1:
        raise ValueError("Stale candle history")
    if any(
        (b.open_time - a.open_time).total_seconds() != interval
        for a, b in pairwise(closed)
    ):
        raise ValueError("Gapped or duplicate candle history")
    if previous is not None and previous.candle.close_time not in {
        b.close_time for b in closed
    }:
        # Downtime longer than the window (or a cache rebuild) leaves unseen bars between
        # the checkpoint and this history. Rebuild causally rather than fail every cycle.
        logger.info(
            "Frame checkpoint outside history timeframe=%s; rebuilding", timeframe
        )
        previous = None
    t = technical(closed, settings.scanner_rvol_baseline)
    tolerance = Decimal(str(t.atr * settings.scanner_zone_atr))
    macro = structure(
        closed,
        settings.scanner_macro_pivot_length,
        tolerance,
        settings.scanner_break_requires_close,
        previous.macro if previous else None,
    )
    micro = structure(
        closed,
        settings.scanner_micro_pivot_length,
        tolerance,
        settings.scanner_break_requires_close,
        previous.micro if previous else None,
    )
    support, resistance = zones(closed, macro.pivots, t.atr, settings.scanner_zone_atr)
    return FrameAnalysis(
        timeframe=timeframe,
        candle=closed[-1],
        technical=t,
        macro=macro,
        micro=micro,
        supports=support,
        resistances=resistance,
        fvgs=fair_value_gaps(
            closed,
            timeframe,
            settings.fvg_min_atr,
            previous.fvgs if previous else None,
            previous.candle.close_time if previous else None,
        ),
    )
