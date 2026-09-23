from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.db.models import Portfolio, Position
from app.services.portfolio_service import PortfolioService


def raise_integrity_http_error(exc: IntegrityError) -> None:
    detail = str(exc.orig)
    if "portfolios_name_key" in detail:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Portfolio with this name already exists.",
        ) from exc
    if "assets_symbol_key" in detail:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset with this symbol already exists.",
        ) from exc
    if "uq_portfolio_asset" in detail:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This asset already exists inside the selected portfolio.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="A database integrity error occurred.",
    ) from exc


def portfolio_or_404(service: PortfolioService, portfolio_id: UUID) -> Portfolio:
    portfolio = service.get_portfolio(portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found.")
    return portfolio


def position_or_404(service: PortfolioService, portfolio_id: UUID, position_id: UUID) -> Position:
    position = service.get_position(portfolio_id, position_id)
    if position is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found.")
    return position
