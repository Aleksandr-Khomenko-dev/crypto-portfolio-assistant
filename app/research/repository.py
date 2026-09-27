from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.scanner_models import MarketSetup, SetupOutcome
from app.research.calibration import OutcomeRecord, calibrate
from app.research.outcomes import KEYS, TRACKING, build_plan
from app.research.schemas import CalibrationReport, OutcomeRead
from app.scanner.domain import Readiness, ScannerResult, Setup


@dataclass(frozen=True)
class OutcomeFilters:
    direction: str | None = None
    symbol: str | None = None
    score_min: int | None = None
    score_max: int | None = None
    regime: str | None = None
    timeframe: str | None = None

    def as_dict(self) -> dict[str, str | int]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def start_outcome(
    session: Session,
    setup_id: UUID,
    candidate: Setup,
    result: ScannerResult,
    snapshot_id: UUID,
    timeframe: str = "1h",
) -> SetupOutcome | None:
    """Begin tracking at the episode's FIRST READY closed candle; later READYs are no-ops."""
    if candidate.readiness != Readiness.READY or candidate.risk.invalidation is None:
        return None
    if session.scalar(
        select(SetupOutcome.id).where(SetupOutcome.market_setup_id == setup_id)
    ):
        return None
    plan = build_plan(
        candidate.direction, timeframe, result.price, candidate.risk.invalidation
    )
    if plan is None:
        return None
    hourly = result.frames["1h"].macro.trend
    row = SetupOutcome(
        market_setup_id=setup_id,
        ready_snapshot_id=snapshot_id,
        timeframe=timeframe,
        ready_at=result.candle_closed_at,
        entry_reference_price=plan.entry,
        invalidation_price=plan.invalidation,
        initial_risk_distance=plan.risk,
        score_at_entry=candidate.score,
        score_breakdown=dict(candidate.blocks),
        market_regime=result.context.state,
        structure_regime="TREND" if hourly in ("BULLISH", "BEARISH") else "RANGE",
        **{f"target_{key}_price": plan.targets[key] for key in KEYS},
        max_favorable_price=plan.entry,
        max_adverse_price=plan.entry,
        # Cursor at the READY candle: only candles opening after it are evaluated.
        last_processed_at=result.candle_closed_at,
        created_at=result.created_at,
        updated_at=result.created_at,
        **{f"hit_{key}": False for key in KEYS},
        **{f"first_{key}": "PENDING" for key in KEYS},
        hit_minus_1r=False,
        bars_processed=0,
        max_favorable_excursion_r=0.0,
        max_adverse_excursion_r=0.0,
        outcome_status=TRACKING,
    )
    session.add(row)
    return row


class ResearchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _query(self, filters: OutcomeFilters) -> Select[Any]:
        query = select(SetupOutcome, MarketSetup).join(
            MarketSetup, SetupOutcome.market_setup_id == MarketSetup.id
        )
        if filters.direction:
            query = query.where(MarketSetup.direction == filters.direction.upper())
        if filters.symbol:
            query = query.where(MarketSetup.symbol == normalize_symbol(filters.symbol))
        if filters.score_min is not None:
            query = query.where(SetupOutcome.score_at_entry >= filters.score_min)
        if filters.score_max is not None:
            query = query.where(SetupOutcome.score_at_entry <= filters.score_max)
        if filters.regime:
            regime = filters.regime.upper()
            query = query.where(
                or_(
                    SetupOutcome.market_regime == regime,
                    SetupOutcome.structure_regime == regime,
                )
            )
        if filters.timeframe:
            query = query.where(SetupOutcome.timeframe == filters.timeframe)
        return query

    def outcomes(
        self, filters: OutcomeFilters, status: str | None = None, limit: int = 100
    ) -> list[OutcomeRead]:
        query = self._query(filters)
        if status:
            query = query.where(SetupOutcome.outcome_status == status.upper())
        rows = self.session.execute(
            query.order_by(SetupOutcome.ready_at.desc()).limit(limit)
        ).all()
        return [read(outcome, setup) for outcome, setup in rows]

    def for_setup(self, setup_id: UUID) -> OutcomeRead | None:
        row = self.session.execute(
            self._query(OutcomeFilters()).where(
                SetupOutcome.market_setup_id == setup_id
            )
        ).first()
        return read(*row) if row else None

    def records(self, filters: OutcomeFilters) -> list[OutcomeRecord]:
        query = self._query(filters).where(SetupOutcome.outcome_status != TRACKING)
        return [
            OutcomeRecord(
                symbol=setup.symbol,
                direction=setup.direction,
                timeframe=outcome.timeframe,
                market_regime=outcome.market_regime,
                structure_regime=outcome.structure_regime,
                score=outcome.score_at_entry,
                status=outcome.outcome_status,
                first={key: getattr(outcome, f"first_{key}") for key in KEYS},
                mfe_r=outcome.max_favorable_excursion_r,
                mae_r=outcome.max_adverse_excursion_r,
                exchange=setup.exchange,
            )
            for outcome, setup in self.session.execute(query).all()
        ]

    def tracking_count(self, filters: OutcomeFilters) -> int:
        query = self._query(filters).where(SetupOutcome.outcome_status == TRACKING)
        return (
            self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        )


def calibration_report(
    session: Session,
    settings: Settings,
    filters: OutcomeFilters,
    group_by: list[str] | None = None,
) -> CalibrationReport:
    repository = ResearchRepository(session)
    group_by = group_by or []
    return CalibrationReport(
        min_samples=settings.calibration_min_samples,
        group_by=group_by,
        filters=filters.as_dict(),
        tracking_count=repository.tracking_count(filters),
        groups=calibrate(
            repository.records(filters),
            settings.calibration_band_edges,
            group_by,
            settings.calibration_min_samples,
        ),
    )


def normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith("USDT") else symbol + "USDT"


def read(outcome: SetupOutcome, setup: MarketSetup) -> OutcomeRead:
    data = {c.key: getattr(outcome, c.key) for c in SetupOutcome.__table__.columns}
    return OutcomeRead.model_validate(
        {
            **data,
            "symbol": setup.symbol,
            "exchange": setup.exchange,
            "direction": setup.direction,
            "setup_created_at": utc(setup.created_at),
            **{k: utc(v) for k, v in data.items() if isinstance(v, datetime)},
        }
    )


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
