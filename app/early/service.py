"""I/O around the pure early engine: closed-candle context, persistence, outcomes.

Context: 15m/1h/4h frames are taken from the scanner's in-memory closed-candle
analysis when fresh (read-only; the scanner's checkpoints are never written), else
computed with the SAME `analyze_frame` from the shared provider's cached closed
candles. 5m frames are computed here from closed 5m candles only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.analytics.analysis import analyze_frame
from app.config import Settings
from app.db.scanner_models import (
    EarlyEventOutcome,
    FastMarketEvent,
    MarketStructureEpisode,
    OpenInterestSnapshot,
    PatternCandidateRecord,
    ScannerSnapshot,
)
from app.early.context import MAJORS, StructureContext, build_context
from app.early.model import BreakoutEpisode, EarlyConfig, KeyZone, PatternCandidate
from app.early.outcomes import EventRecord, measure, successor
from app.early.policy import PolicyConfig
from app.scanner.domain import FrameAnalysis, Timeframe

logger = logging.getLogger(__name__)
HTF: tuple[Timeframe, ...] = ("15m", "1h", "4h")
FramesSource = Callable[[str], dict[str, FrameAnalysis] | None]


def config_from(settings: Settings) -> EarlyConfig:
    return EarlyConfig(
        zone_min_touches=settings.early_zone_min_touches,
        approach_atr=settings.early_approach_atr,
        break_atr=settings.early_break_atr,
        break_hold_seconds=settings.early_break_hold_seconds,
        retest_atr=settings.early_retest_atr,
        fail_atr=settings.early_fail_atr,
        late_origin_atr=settings.early_late_origin_atr,
        ignition_min_strength=settings.early_ignition_min_strength,
        cooldown_minutes=settings.early_cooldown_minutes,
        approach_notify_atr=settings.early_notify_approach_atr,
    )


def policy_config_from(settings: Settings) -> PolicyConfig:
    return PolicyConfig(
        approach_notify_atr=settings.early_notify_approach_atr,
        budget_per_5min=settings.early_notify_budget_per_5min,
        budget_per_hour=settings.early_notify_budget_per_hour,
        notify_zone_watch=settings.early_notify_zone_watch,
        notify_formation=settings.early_notify_formation,
    )


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ContextBuilder:
    def __init__(
        self,
        settings: Settings,
        bingx,
        sessions: sessionmaker[Session],
        cfg: EarlyConfig,
        frames_source: FramesSource | None = None,
    ) -> None:
        self.settings, self.bingx, self.sessions, self.cfg = (
            settings,
            bingx,
            sessions,
            cfg,
        )
        self.frames_source = frames_source
        self.five: dict[str, FrameAnalysis] = {}
        self.own: dict[str, dict[str, FrameAnalysis]] = {}
        self.frames_reused = 0
        self.frames_computed = 0

    async def htf_frames(self, symbol: str, now: datetime) -> dict[str, FrameAnalysis]:
        scanner = self.frames_source(symbol) if self.frames_source else None
        if (
            scanner
            and all(tf in scanner for tf in HTF)
            and now - scanner["15m"].candle.close_time <= timedelta(minutes=45)
        ):
            self.frames_reused += 1
            return scanner
        bars = await asyncio.gather(
            *(self.bingx.candles(symbol, tf, now) for tf in HTF)
        )
        previous = self.own.get(symbol, {})
        frames: dict[str, FrameAnalysis] = await asyncio.to_thread(
            lambda: {
                str(tf): analyze_frame(b, tf, now, self.settings, previous.get(tf))
                for tf, b in zip(HTF, bars, strict=True)
            }
        )
        self.own[symbol] = frames
        self.frames_computed += 1
        return frames

    def _stored_inputs(
        self, symbol: str, now: datetime
    ) -> tuple[float | None, float | None, str | None]:
        """OI 15m change from stored boundary snapshots; funding from the latest
        closed-candle scanner snapshot. None when missing or stale (never invented)."""
        oi = rate = state = None
        with self.sessions() as session:
            rows = session.scalars(
                select(OpenInterestSnapshot)
                .where(
                    OpenInterestSnapshot.exchange == "BINGX",
                    OpenInterestSnapshot.symbol == symbol,
                    OpenInterestSnapshot.bucket_at >= now - timedelta(minutes=45),
                )
                .order_by(OpenInterestSnapshot.bucket_at.desc())
                .limit(2)
            ).all()
            if (
                len(rows) == 2
                and utc(rows[0].bucket_at) - utc(rows[1].bucket_at)
                == timedelta(minutes=15)
                and rows[1].open_interest
            ):
                oi = round(
                    float(rows[0].open_interest / rows[1].open_interest - 1) * 100, 3
                )
            snapshot = session.scalars(
                select(ScannerSnapshot)
                .where(
                    ScannerSnapshot.symbol == symbol,
                    ScannerSnapshot.exchange == "BINGX",
                    ScannerSnapshot.created_at >= now - timedelta(minutes=30),
                )
                .order_by(ScannerSnapshot.created_at.desc())
                .limit(1)
            ).first()
            if snapshot is not None:
                derivatives = snapshot.data.get("derivatives") or {}
                if derivatives.get("funding_rate") is not None:
                    rate = float(derivatives["funding_rate"])
                    state = derivatives.get("funding_state")
        return oi, rate, state

    async def majors(self, now: datetime) -> dict[str, dict[str, FrameAnalysis]]:
        result = {}
        for symbol in MAJORS:
            try:
                result[symbol] = await self.htf_frames(symbol, now)
            except Exception as exc:  # noqa: BLE001 - BTC/ETH context is optional
                logger.debug(
                    "Early majors context failed %s %s", symbol, type(exc).__name__
                )
        return result

    async def build(
        self,
        symbol: str,
        now: datetime,
        majors: dict[str, dict[str, FrameAnalysis]],
        spread_pct: float | None,
    ) -> StructureContext:
        bars5, bars15 = await asyncio.gather(
            self.bingx.candles(symbol, "5m", now),
            self.bingx.candles(symbol, "15m", now),
        )
        frames = await self.htf_frames(symbol, now)
        previous = self.five.get(symbol)
        five = await asyncio.to_thread(
            analyze_frame, bars5, "5m", now, self.settings, previous
        )
        self.five[symbol] = five
        oi, rate, state = await asyncio.to_thread(self._stored_inputs, symbol, now)
        return await asyncio.to_thread(
            build_context,
            symbol,
            frames,
            five,
            bars5[-120:],
            [b for b in bars15 if b.close_time <= frames["15m"].candle.close_time][
                -80:
            ],
            self.cfg,
            majors=majors,
            oi_change_15m=oi,
            funding_rate=rate,
            funding_state=state,
            spread_pct=spread_pct,
        )


# ---- persistence ------------------------------------------------------------------


def _at(epoch: float | None) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC)


def save_episode(
    session: Session, ep: BreakoutEpisode, exchange: str = "BINGX"
) -> None:
    row = session.get(MarketStructureEpisode, ep.id)
    ref = ep.zone.atr or 1.0
    values: dict[str, Any] = {
        "phase": ep.phase,
        "late": ep.late,
        "max_extension_pct": round(ep.max_extension / ep.level * 100, 4)
        if ep.level
        else 0.0,
        "max_extension_atr": round(ep.max_extension / ref, 4),
        "retest_started_at": _at(ep.retest_started),
        "retest_extreme": ep.retest_extreme,
        "momentum_sent": ep.momentum_sent,
        "history": list(ep.history),
        "updated_at": _at(ep.updated) or datetime.now(UTC),
        "closed_at": _at(ep.updated)
        if ep.phase in ("CONFIRMED", "FAILED", "COOLED_DOWN")
        else None,
    }
    if row is None:
        session.add(
            MarketStructureEpisode(
                id=ep.id,
                exchange=exchange,
                symbol=ep.symbol,
                direction=ep.direction,
                kind="BREAKOUT",
                zone=ep.zone.as_dict(),
                zone_timeframe=ep.zone.timeframe,
                zone_type=ep.zone.kind,
                zone_lower=ep.zone.lower,
                zone_upper=ep.zone.upper,
                zone_created_at=ep.zone.created_at,
                touch_count=ep.zone.touches,
                level=ep.level,
                break_time=_at(ep.break_time),
                break_price=ep.break_price,
                origin_price=ep.origin_price,
                started_at=_at(ep.break_time),
                **values,
            )
        )
    else:
        for key, value in values.items():
            setattr(row, key, value)


def load_open_episodes(session: Session, max_hours: float) -> list[BreakoutEpisode]:
    """Open episodes updated recently; stale ones are left closed (no replays)."""
    since = datetime.now(UTC) - timedelta(hours=max_hours)
    rows = session.scalars(
        select(MarketStructureEpisode).where(
            MarketStructureEpisode.phase.in_(("BROKEN", "RETESTING")),
            MarketStructureEpisode.updated_at >= since,
        )
    ).all()
    episodes = []
    for row in rows:
        ep = BreakoutEpisode(
            symbol=row.symbol,
            direction=row.direction,
            zone=KeyZone.from_dict(row.zone),
            level=row.level,
            break_time=utc(row.break_time).timestamp(),
            break_price=row.break_price,
            phase=row.phase,
            max_extension=row.max_extension_atr * (row.zone.get("atr") or 0.0),
            retest_started=utc(row.retest_started_at).timestamp()
            if row.retest_started_at
            else None,
            retest_extreme=row.retest_extreme,
            momentum_sent=row.momentum_sent,
            late=row.late,
            origin_price=row.origin_price,
            updated=utc(row.updated_at).timestamp(),
            id=row.id,
            history=list(row.history or []),
        )
        episodes.append(ep)
    return episodes


def save_pattern(
    session: Session, symbol: str, pattern: PatternCandidate, status: str, at: datetime
) -> None:
    row = session.scalars(
        select(PatternCandidateRecord)
        .where(
            PatternCandidateRecord.symbol == symbol,
            PatternCandidateRecord.pattern_key == pattern.key,
        )
        .order_by(PatternCandidateRecord.detected_at.desc())
        .limit(1)
    ).first()
    if row is not None and row.status == "ACTIVE":
        row.status, row.updated_at = status, at
        return
    if status != "ACTIVE":
        return  # only candidates that were alerted are tracked in the table
    session.add(
        PatternCandidateRecord(
            exchange="BINGX",
            symbol=symbol,
            pattern_key=pattern.key,
            pattern_type=pattern.pattern_type,
            direction=pattern.direction,
            source_timeframe=pattern.source_timeframe,
            formation_started_at=pattern.formation_started_at,
            detected_at=at,
            boundary_level=pattern.boundary_level,
            secondary_boundary=pattern.secondary_boundary,
            touch_count=pattern.touch_count,
            compression_ratio=pattern.compression_ratio,
            distance_to_trigger_atr=pattern.distance_to_trigger_atr,
            formation_strength=pattern.formation_strength,
            invalidation_condition=pattern.invalidation_condition[:120],
            invalidation_level=pattern.invalidation_level,
            evidence=list(pattern.evidence),
            status="ACTIVE",
            updated_at=at,
        )
    )


# ---- outcomes ---------------------------------------------------------------------


class EarlyOutcomeService:
    """Measures matured early events with LATER closed 5m candles (research only)."""

    def __init__(
        self, settings: Settings, sessions: sessionmaker[Session], bingx
    ) -> None:
        self.settings, self.sessions, self.bingx = settings, sessions, bingx
        self.horizon = settings.early_outcome_horizon_minutes

    def _due(self, now: datetime, limit: int) -> list[FastMarketEvent]:
        measured = select(EarlyEventOutcome.event_id)
        with self.sessions() as session:
            rows = session.scalars(
                select(FastMarketEvent)
                .where(
                    FastMarketEvent.event_type.is_not(None),
                    FastMarketEvent.detected_at
                    <= now - timedelta(minutes=self.horizon),
                    FastMarketEvent.id.not_in(measured),
                )
                .order_by(FastMarketEvent.detected_at)
                .limit(limit)
            ).all()
            return [r for r in rows if (r.context or {}).get("mode", "LIVE") == "LIVE"]

    def _later(self, row: FastMarketEvent) -> list[EventRecord]:
        start = utc(row.detected_at)
        with self.sessions() as session:
            later = session.scalars(
                select(FastMarketEvent).where(
                    FastMarketEvent.symbol == row.symbol,
                    FastMarketEvent.detected_at > start,
                    FastMarketEvent.detected_at
                    <= start + timedelta(minutes=self.horizon),
                    FastMarketEvent.event_type.is_not(None),
                )
            ).all()
        return [
            EventRecord(
                str(r.id),
                r.symbol,
                r.direction,
                r.event_type or "",
                utc(r.detected_at),
                str(r.episode_id) if r.episode_id else None,
            )
            for r in later
        ]

    async def run(self, now: datetime | None = None, limit: int = 20) -> int:
        now = now or datetime.now(UTC)
        done = 0
        for row in await asyncio.to_thread(self._due, now, limit):
            detected = utc(row.detected_at)
            bars = await self.bingx.range_candles(
                row.symbol,
                "5m",
                detected,
                detected + timedelta(minutes=self.horizon),
                limit=self.horizon // 5 + 2,
            )
            invalidation = (row.metrics or {}).get("invalidation")
            outcome = measure(row.direction, row.price, invalidation, detected, bars)
            record = EventRecord(
                str(row.id),
                row.symbol,
                row.direction,
                row.event_type or "",
                detected,
                str(row.episode_id) if row.episode_id else None,
            )
            next_type, minutes = successor(
                record, await asyncio.to_thread(self._later, row), self.horizon
            )
            await asyncio.to_thread(
                self._save, row, outcome, invalidation, next_type, minutes, now
            )
            done += 1
        return done

    def _save(self, row, outcome, invalidation, next_type, minutes, now) -> None:
        with self.sessions() as session, session.begin():
            session.add(
                EarlyEventOutcome(
                    event_id=row.id,
                    event_type=row.event_type,
                    symbol=row.symbol,
                    direction=row.direction,
                    detected_at=row.detected_at,
                    measured_at=now,
                    horizon_minutes=self.horizon,
                    entry_price=row.price,
                    invalidation=invalidation,
                    next_event_type=next_type,
                    minutes_to_next_event=minutes,
                    **outcome.as_dict(),
                )
            )


def outcome_rows(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "event_type": r.event_type,
            "first_1r": r.first_1r,
            "first_2r": r.first_2r,
            "mfe_pct": r.mfe_pct,
            "mae_pct": r.mae_pct,
            "mfe_r": r.mfe_r,
            "mae_r": r.mae_r,
            "continuation": r.continuation,
            "next_event_type": r.next_event_type,
            "minutes_to_next_event": r.minutes_to_next_event,
            "minutes_to_1r": r.minutes_to_1r,
            "minutes_to_invalidation": r.minutes_to_invalidation,
        }
        for r in session.scalars(select(EarlyEventOutcome)).all()
    ]
