"""Read-only research endpoints: setup outcomes and historical observed rates."""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db
from app.research.calibration import GROUP_FIELDS
from app.research.repository import (
    OutcomeFilters,
    ResearchRepository,
    calibration_report,
)
from app.research.schemas import CalibrationReport, OutcomeRead
from app.scanner.domain import Direction

Database = Annotated[Session, Depends(get_db)]
Configuration = Annotated[Settings, Depends(get_settings)]

router = APIRouter(prefix="/research", tags=["research"])


OutcomeStatus = Literal[
    "TRACKING",
    "COMPLETED_2R",
    "INVALIDATED",
    "AMBIGUOUS_SAME_BAR",
    "EXPIRED",
    "DATA_GAP",
]
Score = Annotated[int | None, Query(ge=0, le=100)]
Regime = Annotated[str | None, Query(pattern=r"^[A-Za-z_]{1,32}$")]


def common_filters(
    direction: Direction | None = None,
    score_min: Score = None,
    score_max: Score = None,
    regime: Regime = None,
    timeframe: Literal["15m", "1h", "4h"] | None = None,
) -> OutcomeFilters:
    return OutcomeFilters(
        direction=direction,
        score_min=score_min,
        score_max=score_max,
        regime=regime,
        timeframe=timeframe,
    )


Common = Annotated[OutcomeFilters, Depends(common_filters)]


def outcome_filters(
    common: Common,
    symbol: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_]{1,40}$")] = None,
) -> OutcomeFilters:
    return replace(common, symbol=symbol)


Filters = Annotated[OutcomeFilters, Depends(outcome_filters)]


def parse_group_by(group_by: str | None = None) -> list[str]:
    fields = [f.strip() for f in (group_by or "").split(",") if f.strip()]
    unknown = sorted(set(fields) - set(GROUP_FIELDS))
    if unknown:
        raise HTTPException(
            422, f"Unsupported group_by {unknown}; allowed: {list(GROUP_FIELDS)}"
        )
    return fields


GroupBy = Annotated[list[str], Depends(parse_group_by)]


@router.get("/outcomes", response_model=list[OutcomeRead])
def outcomes(
    db: Database,
    filters: Filters,
    status: OutcomeStatus | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> list[OutcomeRead]:
    return ResearchRepository(db).outcomes(filters, status, limit)


@router.get("/calibration", response_model=CalibrationReport)
def calibration(
    db: Database, settings: Configuration, filters: Filters, group_by: GroupBy
) -> CalibrationReport:
    return calibration_report(db, settings, filters, group_by)


@router.get("/calibration/{symbol}", response_model=CalibrationReport)
def symbol_calibration(
    symbol: Annotated[str, Path(pattern=r"^[A-Za-z0-9_]{1,40}$")],
    db: Database,
    settings: Configuration,
    common: Common,
    group_by: GroupBy,
) -> CalibrationReport:
    return calibration_report(db, settings, replace(common, symbol=symbol), group_by)


@router.get("/setup/{setup_id}/outcome", response_model=OutcomeRead)
def setup_outcome(setup_id: UUID, db: Database) -> OutcomeRead:
    outcome = ResearchRepository(db).for_setup(setup_id)
    if outcome is None:
        raise HTTPException(404, "Setup has no outcome; tracking starts at first READY")
    return outcome
