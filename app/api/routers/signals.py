from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.routers._helpers import portfolio_or_404
from app.db.session import get_db
from app.schemas.common import DailyDigestRead, SignalRead
from app.services.portfolio_service import PortfolioService
from app.services.signal_service import SignalService

router = APIRouter(tags=["signals"])


@router.get("/portfolios/{portfolio_id}/signals", response_model=list[SignalRead])
def get_portfolio_signals(portfolio_id: UUID, db: Session = Depends(get_db)) -> list[SignalRead]:
    service = PortfolioService(db)
    portfolio_or_404(service, portfolio_id)
    signals = SignalService(db).list_recent_signals(portfolio_id)
    return [SignalRead.model_validate(signal) for signal in signals]


@router.get("/signals/{signal_id}", response_model=SignalRead)
def get_signal(signal_id: UUID, db: Session = Depends(get_db)) -> SignalRead:
    signal = SignalService(db).get_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found.")
    return SignalRead.model_validate(signal)


@router.get("/portfolios/{portfolio_id}/digests", response_model=list[DailyDigestRead])
def get_portfolio_digests(portfolio_id: UUID, db: Session = Depends(get_db)) -> list[DailyDigestRead]:
    portfolio = portfolio_or_404(PortfolioService(db), portfolio_id)
    return [DailyDigestRead.model_validate(digest) for digest in portfolio.digests[:10]]
