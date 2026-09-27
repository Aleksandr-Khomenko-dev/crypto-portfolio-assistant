from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_scanner_runtime
from app.config import Settings, get_settings
from app.db.session import get_db
from app.scanner.domain import (
    Direction,
    RunRead,
    ScannerResult,
    ScannerStatus,
    SetupRead,
    SignalState,
)
from app.scanner.repository import ScannerRepository
from app.services.scanner_service import (
    ScannerBusyError,
    ScannerRuntime,
    ScannerService,
)

Database = Annotated[Session, Depends(get_db)]
Configuration = Annotated[Settings, Depends(get_settings)]
Runtime = Annotated[ScannerRuntime, Depends(get_scanner_runtime)]

router = APIRouter(prefix="/scanner", tags=["scanner"])


@router.get("/status", response_model=ScannerStatus)
def status(db: Database, runtime: Runtime) -> ScannerStatus:
    run = ScannerRepository(db).last_run()
    return ScannerStatus(
        enabled=runtime.settings.scanner_enabled,
        running=runtime.lock.locked(),
        last_run=RunRead.model_validate(run) if run else None,
    )


@router.get("/setups", response_model=list[SetupRead])
def setups(
    db: Database,
    minimum_score: int = Query(0, ge=0, le=100),
    direction: Direction | None = None,
    state: SignalState | None = None,
    symbol: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[SetupRead]:
    return ScannerRepository(db).setups(
        now=datetime.now(UTC),
        minimum_score=minimum_score,
        direction=direction,
        state=state,
        symbol=symbol,
        limit=limit,
    )


@router.get("/setups/{symbol}", response_model=ScannerResult)
def detail(symbol: str, db: Database) -> ScannerResult:
    results = ScannerRepository(db).latest_results(symbol, limit=1)
    if not results:
        raise HTTPException(404, "Symbol has no scanner snapshot")
    return results[0]


@router.get("/snapshots", response_model=list[ScannerResult])
def snapshots(
    db: Database, limit: int = Query(100, ge=1, le=500)
) -> list[ScannerResult]:
    return ScannerRepository(db).latest_results(limit=limit)


@router.post("/run", response_model=RunRead)
async def run(db: Database, runtime: Runtime) -> RunRead:
    try:
        return await ScannerService(db, runtime).run()
    except ScannerBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/top/{direction}", response_model=list[SetupRead])
def top(
    direction: str,
    db: Database,
    settings: Configuration,
    limit: int = Query(10, ge=1, le=100),
) -> list[SetupRead]:
    if direction.lower() not in ("long", "short"):
        raise HTTPException(422, "Direction must be long or short")
    return ScannerRepository(db).setups(
        now=datetime.now(UTC),
        direction=Direction(direction.upper()),
        minimum_score=settings.scanner_watch_score,
        limit=limit,
    )


@router.get("/oi-collection")
def oi_collection(runtime: Runtime) -> dict:
    """Latest collector telemetry and exact-horizon coverage (diagnostic only)."""
    from app.analytics.open_interest import next_boundary

    return {
        "running": runtime.oi_lock.locked(),
        "next_boundary": next_boundary(datetime.now(UTC)),
        "last_collection": runtime.oi_last_collection,
    }


@router.get("/early/events")
def early_events(
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    event_type: str | None = None,
) -> list[dict]:
    """Recent early-warning events (research log; never a trading score)."""
    from sqlalchemy import select

    from app.db.scanner_models import FastMarketEvent

    query = select(FastMarketEvent).order_by(FastMarketEvent.detected_at.desc())
    if event_type:
        query = query.where(FastMarketEvent.event_type == event_type.upper())
    return [
        {
            "id": str(row.id),
            "symbol": row.symbol,
            "direction": row.direction,
            "event_type": row.event_type or row.state,
            "decision": row.decision,
            "price": row.price,
            "strength": row.strength,
            "source_timeframe": row.source_timeframe,
            "zone": row.zone,
            "metrics": row.metrics,
            "technical_score": row.technical_score,
            "detected_at": row.detected_at,
            "sent": row.sent,
            "telegram_message_id": row.telegram_message_id,
            "latency_ms": row.latency_ms,
        }
        for row in db.scalars(query.limit(limit))
    ]


@router.get("/early/calibration")
def early_calibration(db: Database, settings: Configuration) -> dict:
    """Measured early-event statistics. Rates are withheld below the minimum sample
    size; nothing here is a success probability."""
    from app.early.outcomes import calibration
    from app.early.service import outcome_rows

    return {
        "min_samples": settings.calibration_min_samples,
        "horizon_minutes": settings.early_outcome_horizon_minutes,
        "note": "Research statistics, not probabilities. Low samples are not evidence.",
        "event_types": calibration(outcome_rows(db), settings.calibration_min_samples),
    }
