from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.bootstrap import ensure_reference_data
from app.db.models import Portfolio, Position, Signal, StrategyProfile, StrategyProfileCode
from app.schemas.portfolio import PortfolioCreate, PortfolioSummaryRead, PortfolioUpdate, PositionCreate, PositionUpdate
from app.services.asset_service import AssetService


class PortfolioService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.asset_service = AssetService(session)

    def ensure_reference_data(self) -> None:
        ensure_reference_data(self.session)

    def list_portfolios(self) -> list[Portfolio]:
        statement = (
            select(Portfolio)
            .options(
                selectinload(Portfolio.strategy_profile),
                selectinload(Portfolio.positions).selectinload(Position.asset),
                selectinload(Portfolio.signals),
            )
            .order_by(Portfolio.name.asc())
        )
        return list(self.session.scalars(statement).unique())

    def get_portfolio(self, portfolio_id: UUID) -> Portfolio | None:
        statement = (
            select(Portfolio)
            .options(
                selectinload(Portfolio.strategy_profile),
                selectinload(Portfolio.positions).selectinload(Position.asset),
                selectinload(Portfolio.signals),
                selectinload(Portfolio.digests),
            )
            .where(Portfolio.id == portfolio_id)
        )
        return self.session.scalars(statement).unique().one_or_none()

    def list_portfolio_summaries(self) -> list[PortfolioSummaryRead]:
        summaries: list[PortfolioSummaryRead] = []
        for portfolio in self.list_portfolios():
            total_cost_basis = sum(
                (Decimal(position.cost_basis) for position in portfolio.positions),
                start=Decimal("0"),
            )
            summaries.append(
                PortfolioSummaryRead(
                    id=portfolio.id,
                    name=portfolio.name,
                    strategy_profile_code=portfolio.strategy_profile.code,
                    position_count=len(portfolio.positions),
                    latest_signal_count=len(portfolio.signals[:10]),
                    total_cost_basis=total_cost_basis,
                )
            )
        return summaries

    def create_portfolio(self, payload: PortfolioCreate) -> Portfolio:
        self.ensure_reference_data()
        strategy_profile = self.get_strategy_profile(payload.strategy_profile_code)
        portfolio = Portfolio(
            name=payload.name,
            description=payload.description,
            base_currency=payload.base_currency,
            strategy_profile_id=strategy_profile.id,
            strategy_profile=strategy_profile,
            is_active=payload.is_active,
            morning_digest_enabled=payload.morning_digest_enabled,
            alerts_enabled=payload.alerts_enabled,
            telegram_chat_id=payload.telegram_chat_id,
            risk_notes=payload.risk_notes,
        )
        self.session.add(portfolio)
        self.session.flush()

        for position_payload in payload.positions:
            self._create_position(portfolio, position_payload)

        self.session.commit()
        return self.get_portfolio(portfolio.id) or portfolio

    def update_portfolio(self, portfolio: Portfolio, payload: PortfolioUpdate) -> Portfolio:
        updates = payload.model_dump(exclude_unset=True)
        if "strategy_profile_code" in updates:
            strategy_profile = self.get_strategy_profile(updates.pop("strategy_profile_code"))
            portfolio.strategy_profile_id = strategy_profile.id
            portfolio.strategy_profile = strategy_profile
        for field, value in updates.items():
            setattr(portfolio, field, value)
        self.session.add(portfolio)
        self.session.commit()
        return self.get_portfolio(portfolio.id) or portfolio

    def delete_portfolio(self, portfolio: Portfolio) -> None:
        self.session.delete(portfolio)
        self.session.commit()

    def add_position(self, portfolio: Portfolio, payload: PositionCreate) -> Position:
        position = self._create_position(portfolio, payload)
        self.session.commit()
        return self.get_position(portfolio.id, position.id) or position

    def get_position(self, portfolio_id: UUID, position_id: UUID) -> Position | None:
        statement = (
            select(Position)
            .options(selectinload(Position.asset))
            .where(Position.portfolio_id == portfolio_id, Position.id == position_id)
        )
        return self.session.scalars(statement).one_or_none()

    def update_position(self, position: Position, payload: PositionUpdate) -> Position:
        updates = payload.model_dump(exclude_unset=True)
        for field, value in updates.items():
            setattr(position, field, value)
        if "quantity" in updates or "average_entry_price" in updates:
            quantity = Decimal(position.quantity)
            average_entry_price = Decimal(position.average_entry_price)
            position.cost_basis = updates.get("cost_basis") or (quantity * average_entry_price)
        self.session.add(position)
        self.session.commit()
        return self.get_position(position.portfolio_id, position.id) or position

    def delete_position(self, position: Position) -> None:
        self.session.delete(position)
        self.session.commit()

    def get_strategy_profile(self, code: StrategyProfileCode) -> StrategyProfile:
        statement = select(StrategyProfile).where(StrategyProfile.code == code)
        profile = self.session.scalar(statement)
        if profile is None:
            self.ensure_reference_data()
            profile = self.session.scalar(statement)
        if profile is None:
            raise ValueError(f"Unknown strategy profile: {code}")
        return profile

    def _create_position(self, portfolio: Portfolio, payload: PositionCreate) -> Position:
        asset = self.asset_service.get_or_create_asset(payload.asset)
        position = Position(
            portfolio_id=portfolio.id,
            asset_id=asset.id,
            quantity=payload.quantity,
            average_entry_price=payload.average_entry_price,
            cost_basis=payload.cost_basis or (payload.quantity * payload.average_entry_price),
            target_weight_pct=payload.target_weight_pct,
            notes=payload.notes,
        )
        self.session.add(position)
        self.session.flush()
        return position
