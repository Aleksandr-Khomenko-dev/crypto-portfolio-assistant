from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routers._helpers import portfolio_or_404, position_or_404, raise_integrity_http_error
from app.db.session import get_db
from app.schemas.portfolio import (
    PortfolioCreate,
    PortfolioRead,
    PortfolioSummaryRead,
    PortfolioUpdate,
    PositionCreate,
    PositionRead,
    PositionUpdate,
    TransactionCreate,
    TransactionRead,
)
from app.services.portfolio_service import PortfolioService
from app.services.transaction_service import TransactionService

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


@router.get("", response_model=list[PortfolioSummaryRead])
def list_portfolios(db: Session = Depends(get_db)) -> list[PortfolioSummaryRead]:
    return PortfolioService(db).list_portfolio_summaries()


@router.post("", response_model=PortfolioRead, status_code=status.HTTP_201_CREATED)
def create_portfolio(payload: PortfolioCreate, db: Session = Depends(get_db)) -> PortfolioRead:
    try:
        portfolio = PortfolioService(db).create_portfolio(payload)
    except IntegrityError as exc:
        db.rollback()
        raise_integrity_http_error(exc)
    return PortfolioRead.model_validate(portfolio)


@router.get("/{portfolio_id}", response_model=PortfolioRead)
def get_portfolio(portfolio_id: UUID, db: Session = Depends(get_db)) -> PortfolioRead:
    portfolio = portfolio_or_404(PortfolioService(db), portfolio_id)
    return PortfolioRead.model_validate(portfolio)


@router.patch("/{portfolio_id}", response_model=PortfolioRead)
def update_portfolio(
    portfolio_id: UUID,
    payload: PortfolioUpdate,
    db: Session = Depends(get_db),
) -> PortfolioRead:
    service = PortfolioService(db)
    portfolio = portfolio_or_404(service, portfolio_id)
    updated = service.update_portfolio(portfolio, payload)
    return PortfolioRead.model_validate(updated)


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_portfolio(portfolio_id: UUID, db: Session = Depends(get_db)) -> None:
    service = PortfolioService(db)
    portfolio = portfolio_or_404(service, portfolio_id)
    service.delete_portfolio(portfolio)


# --- Positions ---

@router.post(
    "/{portfolio_id}/positions",
    response_model=PositionRead,
    status_code=status.HTTP_201_CREATED,
)
def add_position(
    portfolio_id: UUID,
    payload: PositionCreate,
    db: Session = Depends(get_db),
) -> PositionRead:
    service = PortfolioService(db)
    portfolio = portfolio_or_404(service, portfolio_id)
    try:
        position = service.add_position(portfolio, payload)
    except IntegrityError as exc:
        db.rollback()
        raise_integrity_http_error(exc)
    return PositionRead.model_validate(position)


@router.patch("/{portfolio_id}/positions/{position_id}", response_model=PositionRead)
def update_position(
    portfolio_id: UUID,
    position_id: UUID,
    payload: PositionUpdate,
    db: Session = Depends(get_db),
) -> PositionRead:
    service = PortfolioService(db)
    portfolio_or_404(service, portfolio_id)
    position = position_or_404(service, portfolio_id, position_id)
    updated = service.update_position(position, payload)
    return PositionRead.model_validate(updated)


@router.delete("/{portfolio_id}/positions/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_position(
    portfolio_id: UUID,
    position_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    service = PortfolioService(db)
    portfolio_or_404(service, portfolio_id)
    position = position_or_404(service, portfolio_id, position_id)
    service.delete_position(position)


# --- Transactions ---

@router.get(
    "/{portfolio_id}/positions/{position_id}/transactions",
    response_model=list[TransactionRead],
)
def list_transactions(
    portfolio_id: UUID,
    position_id: UUID,
    db: Session = Depends(get_db),
) -> list[TransactionRead]:
    service = PortfolioService(db)
    portfolio_or_404(service, portfolio_id)
    position_or_404(service, portfolio_id, position_id)
    transactions = TransactionService(db).list_transactions(position_id)
    return [TransactionRead.model_validate(tx) for tx in transactions]


@router.post(
    "/{portfolio_id}/positions/{position_id}/transactions",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
)
def add_transaction(
    portfolio_id: UUID,
    position_id: UUID,
    payload: TransactionCreate,
    db: Session = Depends(get_db),
) -> TransactionRead:
    service = PortfolioService(db)
    portfolio_or_404(service, portfolio_id)
    position = position_or_404(service, portfolio_id, position_id)
    tx = TransactionService(db).add_transaction(position, payload)
    return TransactionRead.model_validate(tx)


@router.delete(
    "/{portfolio_id}/positions/{position_id}/transactions/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_transaction(
    portfolio_id: UUID,
    position_id: UUID,
    transaction_id: UUID,
    db: Session = Depends(get_db),
) -> None:
    service = PortfolioService(db)
    portfolio_or_404(service, portfolio_id)
    position_or_404(service, portfolio_id, position_id)
    tx_service = TransactionService(db)
    tx = tx_service.get_transaction(transaction_id)
    if tx is None or tx.position_id != position_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found.")
    tx_service.delete_transaction(tx)
