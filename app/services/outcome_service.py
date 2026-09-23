"""Advance open setup outcomes with newly closed candles. Read-only market data."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.scanner_models import MarketSetup, SetupOutcome
from app.providers.futures import FuturesProvider
from app.research.outcomes import (
    LOWER_TIMEFRAME,
    TRACKING,
    advance,
    plan_from_row,
    progress_columns,
    progress_from_row,
)
from app.scanner.domain import Candle, Direction, Timeframe

logger = logging.getLogger(__name__)


class OutcomeService:
    def __init__(
        self, session: Session, provider: FuturesProvider, settings: Settings
    ) -> None:
        self.session, self.provider, self.settings = session, provider, settings

    async def process(
        self,
        now: datetime,
        known_bars: dict[tuple[str, str], list[Candle]] | None = None,
    ) -> Counter[str]:
        """`known_bars` reuses closed candles the scanner already fetched this cycle."""
        stats: Counter[str] = Counter()
        rows = self.session.execute(
            select(SetupOutcome.id, MarketSetup.symbol, SetupOutcome.timeframe)
            .join(MarketSetup, SetupOutcome.market_setup_id == MarketSetup.id)
            .where(
                SetupOutcome.outcome_status == TRACKING,
                # Only this provider's exchange: a setup's outcome is always measured
                # with candles from the exchange that produced it.
                MarketSetup.exchange == self.provider.exchange,
            )
        ).all()
        self.session.commit()  # No read transaction spans the HTTP requests below.
        bars = dict(known_bars or {})
        missing = sorted({(s, tf) for _, s, tf in rows} - set(bars))
        semaphore = asyncio.Semaphore(self.settings.scanner_concurrency)

        async def fetch(symbol: str, timeframe: str) -> None:
            async with semaphore:
                try:
                    bars[(symbol, timeframe)] = await self.provider.candles(
                        symbol, cast(Timeframe, timeframe), now
                    )
                except Exception as exc:  # noqa: BLE001 - isolate per-symbol provider failures
                    stats["outcome_fetch_failures"] += 1
                    logger.warning(
                        "Outcome candles unavailable symbol=%s error=%s",
                        symbol,
                        type(exc).__name__,
                    )

        await asyncio.gather(*(fetch(symbol, tf) for symbol, tf in missing))
        for outcome_id, symbol, timeframe in rows:
            history = bars.get((symbol, timeframe))
            if history is None:
                continue
            try:
                await self._advance_one(outcome_id, symbol, history, now, stats)
            except Exception as exc:  # noqa: BLE001 - one outcome must not stop the others
                self.session.rollback()
                stats["outcome_errors"] += 1
                logger.warning(
                    "Outcome update failed symbol=%s error=%s",
                    symbol,
                    type(exc).__name__,
                )
        return stats

    async def _advance_one(
        self,
        outcome_id: object,
        symbol: str,
        history: list[Candle],
        now: datetime,
        stats: Counter[str],
    ) -> None:
        row = self.session.get(SetupOutcome, outcome_id)
        setup = self.session.get(MarketSetup, row.market_setup_id) if row else None
        if row is None or setup is None or row.outcome_status != TRACKING:
            return
        plan = plan_from_row(row, Direction(setup.direction))
        progress = progress_from_row(row)
        horizon = self.settings.outcome_max_bars(row.timeframe)
        self.session.commit()
        step = advance(plan, progress, history, now, horizon)
        parent = step.unresolved_parent
        if parent is not None and self.settings.outcome_ltf_resolution:
            try:
                children = await self.provider.range_candles(
                    symbol,
                    cast(Timeframe, LOWER_TIMEFRAME[row.timeframe]),
                    parent.open_time,
                    parent.close_time,
                )
                step = advance(
                    plan,
                    progress,
                    history,
                    now,
                    horizon,
                    {parent.open_time: children},
                )
                stats["ambiguity_ltf_attempts"] += 1
                if step.progress.ambiguity_resolution == "LOWER_TIMEFRAME":
                    stats["ambiguity_ltf_resolved"] += 1
            except Exception as exc:  # noqa: BLE001 - stay ambiguous rather than guess
                stats["ambiguity_ltf_failures"] += 1
                logger.warning(
                    "Lower-timeframe replay unavailable symbol=%s error=%s",
                    symbol,
                    type(exc).__name__,
                )
        new = step.progress
        if new == progress:  # Idempotent: no newly closed bars.
            return
        row = self.session.get(SetupOutcome, outcome_id)
        if row is None or row.outcome_status != TRACKING:
            return
        for name, value in progress_columns(new).items():
            setattr(row, name, value)
        row.updated_at = now
        self.session.commit()
        stats["outcomes_updated"] += 1
        if new.outcome_status != TRACKING:
            stats["outcomes_completed"] += 1
            logger.info(
                "Setup outcome completed symbol=%s direction=%s status=%s",
                symbol,
                setup.direction,
                new.outcome_status,
            )
