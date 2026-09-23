from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, model_validator

from app.db.models import StrategyProfileCode, TransactionSide
from app.schemas.common import ORMBaseModel, StrategyProfileRead


class AssetBase(ORMBaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    name: str | None = Field(default=None, max_length=120)
    coingecko_id: str | None = Field(default=None, max_length=120)
    binance_symbol: str | None = Field(default=None, max_length=40)
    bybit_symbol: str | None = Field(default=None, max_length=40)
    metadata_json: dict[str, object] = Field(default_factory=dict)


class AssetCreate(AssetBase):
    pass


class AssetUpdate(ORMBaseModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=20)
    name: str | None = Field(default=None, max_length=120)
    coingecko_id: str | None = Field(default=None, max_length=120)
    binance_symbol: str | None = Field(default=None, max_length=40)
    bybit_symbol: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None
    metadata_json: dict[str, object] | None = None


class AssetRead(AssetBase):
    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PositionBase(ORMBaseModel):
    quantity: Decimal = Field(gt=0)
    average_entry_price: Decimal = Field(ge=0)
    cost_basis: Decimal | None = Field(default=None, ge=0)
    target_weight_pct: Decimal | None = Field(default=None, ge=0, le=100)
    notes: str | None = None

    @model_validator(mode="after")
    def populate_cost_basis(self) -> "PositionBase":
        if self.cost_basis is None:
            self.cost_basis = self.quantity * self.average_entry_price
        return self


class PositionCreate(PositionBase):
    asset: AssetCreate


class PositionUpdate(ORMBaseModel):
    quantity: Decimal | None = Field(default=None, gt=0)
    average_entry_price: Decimal | None = Field(default=None, ge=0)
    cost_basis: Decimal | None = Field(default=None, ge=0)
    target_weight_pct: Decimal | None = Field(default=None, ge=0, le=100)
    notes: str | None = None
    price_alert_take_profit_usd: Decimal | None = Field(default=None, ge=0)
    price_alert_stop_loss_usd: Decimal | None = Field(default=None, ge=0)


class PositionRead(PositionBase):
    id: UUID
    portfolio_id: UUID
    asset_id: UUID
    asset: AssetRead
    price_alert_take_profit_usd: Decimal | None = None
    price_alert_stop_loss_usd: Decimal | None = None
    last_manual_update_at: datetime
    created_at: datetime
    updated_at: datetime


class PortfolioBase(ORMBaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = None
    base_currency: str = Field(default="USD", min_length=3, max_length=10)
    strategy_profile_code: StrategyProfileCode = StrategyProfileCode.MAIN
    is_active: bool = True
    morning_digest_enabled: bool = True
    alerts_enabled: bool = True
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    risk_notes: str | None = None


class PortfolioCreate(PortfolioBase):
    positions: list[PositionCreate] = Field(default_factory=list)


class PortfolioUpdate(ORMBaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = None
    base_currency: str | None = Field(default=None, min_length=3, max_length=10)
    strategy_profile_code: StrategyProfileCode | None = None
    is_active: bool | None = None
    morning_digest_enabled: bool | None = None
    alerts_enabled: bool | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    risk_notes: str | None = None


class PortfolioRead(PortfolioBase):
    id: UUID
    strategy_profile: StrategyProfileRead
    positions: list[PositionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PortfolioSummaryRead(ORMBaseModel):
    id: UUID
    name: str
    strategy_profile_code: StrategyProfileCode
    position_count: int
    latest_signal_count: int
    total_cost_basis: Decimal


class PortfolioRiskSummaryRead(ORMBaseModel):
    portfolio_id: UUID
    portfolio_name: str
    concentration_warnings: list[str] = Field(default_factory=list)
    volatility_warnings: list[str] = Field(default_factory=list)
    risk_summary: str


class TransactionCreate(ORMBaseModel):
    side: TransactionSide
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(gt=0)
    fee_amount: Decimal | None = Field(default=None, ge=0)
    executed_at: datetime | None = None
    notes: str | None = None


class TransactionRead(ORMBaseModel):
    id: UUID
    portfolio_id: UUID
    position_id: UUID | None
    asset_id: UUID
    side: TransactionSide
    quantity: Decimal
    unit_price: Decimal
    fee_amount: Decimal | None
    executed_at: datetime
    notes: str | None
    created_at: datetime
