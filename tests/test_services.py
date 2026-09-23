from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import get_settings
from app.db.models import Portfolio, PriceSnapshot, StrategyProfileCode
from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote
from app.schemas.portfolio import PortfolioCreate, PositionCreate
from app.services.monitoring_service import MonitoringService
from app.services.portfolio_service import PortfolioService


class RisingMarketProvider(AbstractMarketDataProvider):
    provider_name = "test"

    async def fetch_quote(self, query: AssetMarketQuery) -> MarketQuote | None:
        return MarketQuote(
            asset_id=query.asset_id,
            provider=self.provider_name,
            symbol=query.symbol,
            price_usd=Decimal("160"),
            market_cap_usd=Decimal("1000000000"),
            volume_24h_usd=Decimal("50000000"),
            change_1h_pct=Decimal("7"),
            change_24h_pct=Decimal("18"),
            source_timestamp=datetime.now(timezone.utc),
            raw={},
        )

    async def fetch_quotes(self, queries: list[AssetMarketQuery]) -> dict:
        quotes = {}
        for query in queries:
            quote = await self.fetch_quote(query)
            if quote is not None:
                quotes[query.asset_id] = quote
        return quotes


@pytest.mark.asyncio
async def test_monitoring_service_generates_signals_and_digest(session) -> None:
    portfolio_service = PortfolioService(session)
    portfolio = portfolio_service.create_portfolio(
        PortfolioCreate(
            name="Alpha",
            strategy_profile_code=StrategyProfileCode.MAIN,
            positions=[
                PositionCreate(
                    quantity=Decimal("2"),
                    average_entry_price=Decimal("100"),
                    asset={"symbol": "BTC", "name": "Bitcoin", "coingecko_id": "bitcoin"},
                )
            ],
        )
    )

    position = portfolio.positions[0]
    session.add(
        PriceSnapshot(
            asset_id=position.asset_id,
            provider="test",
            price_usd=Decimal("120"),
            market_cap_usd=Decimal("900000000"),
            volume_24h_usd=Decimal("25000000"),
            change_1h_pct=Decimal("3"),
            change_24h_pct=Decimal("6"),
            captured_at=datetime.now(timezone.utc) - timedelta(hours=4, minutes=5),
            raw_payload={},
        )
    )
    session.commit()

    monitoring_service = MonitoringService(session, RisingMarketProvider(), settings=get_settings())
    result = await monitoring_service.evaluate_portfolio(portfolio, run_reason="test")
    digest = await monitoring_service.create_morning_digest(portfolio)

    titles = {signal.title.lower() for signal in result.generated_signals}
    assert result.signal_count >= 3
    assert any("take-profit" in title for title in titles)
    assert any("concentration risk" in title for title in titles)
    assert digest.digest.content.startswith("Alpha morning digest")


@pytest.mark.asyncio
async def test_monitoring_service_handles_zero_cost_airdrop_position(session) -> None:
    portfolio_service = PortfolioService(session)
    portfolio = portfolio_service.create_portfolio(
        PortfolioCreate(
            name="Airdrop",
            strategy_profile_code=StrategyProfileCode.MAIN,
            positions=[
                PositionCreate(
                    quantity=Decimal("49821"),
                    average_entry_price=Decimal("0"),
                    notes="NOT airdrop",
                    asset={"symbol": "NOT", "name": "Notcoin", "coingecko_id": "notcoin"},
                )
            ],
        )
    )

    monitoring_service = MonitoringService(session, RisingMarketProvider(), settings=get_settings())
    result = await monitoring_service.evaluate_portfolio(portfolio, run_reason="test-airdrop")
    digest = await monitoring_service.create_morning_digest(portfolio)

    assert result.evaluation.total_cost_basis == Decimal("0")
    assert result.evaluation.positions[0].unrealized_pnl_pct is None
    assert "Read-only monitor" in digest.digest.content
