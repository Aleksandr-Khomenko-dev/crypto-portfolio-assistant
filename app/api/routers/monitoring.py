from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_market_provider
from app.api.routers._helpers import portfolio_or_404
from app.config import get_settings
from app.db.session import get_db
from app.providers.interfaces import AbstractMarketDataProvider
from app.schemas.monitoring import BulkMonitorResponse, DigestResult, PortfolioMonitorResult
from app.schemas.portfolio import PortfolioRiskSummaryRead
from app.services.monitoring_service import MonitoringService
from app.services.portfolio_service import PortfolioService

router = APIRouter(tags=["monitoring"])


@router.get("/portfolios/{portfolio_id}/risk", response_model=PortfolioRiskSummaryRead)
async def get_portfolio_risk(
    portfolio_id: UUID,
    db: Session = Depends(get_db),
    market_provider: AbstractMarketDataProvider = Depends(get_market_provider),
) -> PortfolioRiskSummaryRead:
    portfolio = portfolio_or_404(PortfolioService(db), portfolio_id)
    metrics = await MonitoringService(db, market_provider, settings=get_settings()).preview_portfolio(portfolio)
    volatility_warnings = [
        f"{position.symbol} elevated volatility"
        for position in metrics.positions
        if max(abs(position.move_24h_pct or Decimal("0")), abs(position.move_4h_pct or Decimal("0"))) >= Decimal("18")
    ]
    return PortfolioRiskSummaryRead(
        portfolio_id=portfolio.id,
        portfolio_name=portfolio.name,
        concentration_warnings=metrics.concentration_warnings,
        volatility_warnings=volatility_warnings,
        risk_summary=metrics.action_summary,
    )


@router.post("/portfolios/{portfolio_id}/monitor", response_model=PortfolioMonitorResult)
async def monitor_portfolio(
    portfolio_id: UUID,
    db: Session = Depends(get_db),
    market_provider: AbstractMarketDataProvider = Depends(get_market_provider),
) -> PortfolioMonitorResult:
    portfolio = portfolio_or_404(PortfolioService(db), portfolio_id)
    return await MonitoringService(db, market_provider, settings=get_settings()).evaluate_portfolio(
        portfolio,
        run_reason="api-manual",
    )


@router.post("/monitor/run", response_model=BulkMonitorResponse)
async def monitor_all_portfolios(
    db: Session = Depends(get_db),
    market_provider: AbstractMarketDataProvider = Depends(get_market_provider),
) -> BulkMonitorResponse:
    return await MonitoringService(db, market_provider, settings=get_settings()).evaluate_all_portfolios(
        run_reason="api-bulk"
    )


@router.post("/portfolios/{portfolio_id}/digest", response_model=DigestResult)
async def generate_digest(
    portfolio_id: UUID,
    db: Session = Depends(get_db),
    market_provider: AbstractMarketDataProvider = Depends(get_market_provider),
) -> DigestResult:
    portfolio = portfolio_or_404(PortfolioService(db), portfolio_id)
    return await MonitoringService(db, market_provider, settings=get_settings()).create_morning_digest(portfolio)
