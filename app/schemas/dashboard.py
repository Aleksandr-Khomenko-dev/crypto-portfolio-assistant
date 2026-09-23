from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.db.models import SignalSeverity, SignalType, TransactionSide
from app.schemas.common import ORMBaseModel


class DashboardPosition(ORMBaseModel):
    id: UUID
    symbol: str
    asset_name: str | None = None
    quantity: Decimal
    average_entry_price: Decimal
    cost_basis: Decimal
    # Live price data (from latest PriceSnapshot)
    current_price: Decimal = Decimal("0")
    current_value: Decimal = Decimal("0")
    pnl_usd: Decimal = Decimal("0")
    pnl_pct: Decimal = Decimal("0")
    change_1h_pct: Decimal | None = None
    change_24h_pct: Decimal | None = None
    price_updated_at: datetime | None = None
    price_alert_take_profit_usd: Decimal | None = None
    price_alert_stop_loss_usd: Decimal | None = None
    target_weight_pct: Decimal | None = None
    notes: str | None = None
    transaction_count: int = 0


class DashboardSignal(ORMBaseModel):
    id: UUID
    portfolio_id: UUID
    signal_type: SignalType
    severity: SignalSeverity
    title: str
    message: str
    action_idea: str
    created_at: datetime


class DashboardTransaction(ORMBaseModel):
    id: UUID
    portfolio_id: UUID
    position_id: UUID | None = None
    asset_symbol: str
    side: TransactionSide
    quantity: Decimal
    unit_price: Decimal
    fee_amount: Decimal | None = None
    notes: str | None = None
    executed_at: datetime


class DashboardDigestPreview(ORMBaseModel):
    id: UUID
    created_at: datetime
    sent_to_telegram: bool
    preview: str
    digest: DashboardDigestContent


class DashboardDigestContent(ORMBaseModel):
    content: str


class DashboardDigestPreview(ORMBaseModel):
    id: UUID
    created_at: datetime
    sent_to_telegram: bool
    preview: str
    digest: DashboardDigestContent


class DashboardRecentDigest(ORMBaseModel):
    portfolio_id: UUID
    portfolio_name: str
    created_at: datetime
    sent_to_telegram: bool
    preview: str


class DashboardPortfolio(ORMBaseModel):
    id: UUID
    name: str
    description: str | None = None
    base_currency: str
    strategy_profile_code: str
    strategy_profile_name: str
    strategy_profile_description: str
    is_active: bool
    alerts_enabled: bool
    morning_digest_enabled: bool
    telegram_chat_configured: bool
    risk_notes: str | None = None
    total_cost_basis: Decimal
    total_current_value: Decimal = Decimal("0")
    total_pnl_usd: Decimal = Decimal("0")
    total_pnl_pct: Decimal = Decimal("0")
    total_change_24h_pct: Decimal | None = None
    position_count: int
    latest_signal_count: int
    positions: list[DashboardPosition] = Field(default_factory=list)
    recent_signals: list[DashboardSignal] = Field(default_factory=list)
    recent_transactions: list[DashboardTransaction] = Field(default_factory=list)
    latest_digest: DashboardDigestPreview | None = None


class DashboardTotals(ORMBaseModel):
    portfolio_count: int
    position_count: int
    tracked_asset_count: int
    total_cost_basis: Decimal
    total_current_value: Decimal = Decimal("0")
    total_pnl_usd: Decimal = Decimal("0")
    total_pnl_pct: Decimal = Decimal("0")
    active_signal_count: int
    transaction_count: int


class DashboardOverview(ORMBaseModel):
    environment: str
    data_source: str
    provider_order: list[str]
    scheduler_enabled: bool
    telegram_enabled: bool
    timezone: str
    default_quote_currency: str
    totals: DashboardTotals
    portfolios: list[DashboardPortfolio]
    recent_signals: list[DashboardSignal]
    recent_digests: list[DashboardRecentDigest]


# Backward-compatible aliases used by app/schemas/__init__.py
DashboardDigestRead = DashboardDigestPreview
DashboardOverviewRead = DashboardOverview
DashboardPortfolioRead = DashboardPortfolio
DashboardPositionRead = DashboardPosition
DashboardTotalsRead = DashboardTotals
DashboardTransactionRead = DashboardTransaction
