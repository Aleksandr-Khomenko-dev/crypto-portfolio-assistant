from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.db.models import StrategyProfileCode
from app.schemas.common import DailyDigestRead, ORMBaseModel, SignalRead


class PositionMetricsRead(ORMBaseModel):
    position_id: UUID
    asset_id: UUID
    symbol: str
    asset_name: str | None = None
    quantity: Decimal
    average_entry_price: Decimal
    cost_basis: Decimal
    current_price: Decimal
    position_value: Decimal
    unrealized_pnl_value: Decimal
    unrealized_pnl_pct: Decimal | None = None
    weight_pct: Decimal | None = None
    move_15m_pct: Decimal | None = None
    move_1h_pct: Decimal | None = None
    move_4h_pct: Decimal | None = None
    move_24h_pct: Decimal | None = None
    peak_price_48h: Decimal | None = None
    drop_from_peak_pct: Decimal | None = None
    market_cap_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    provider: str


class PortfolioEvaluationRead(ORMBaseModel):
    portfolio_id: UUID
    portfolio_name: str
    strategy_profile_code: StrategyProfileCode
    evaluated_at: datetime
    total_value: Decimal
    total_cost_basis: Decimal
    unrealized_pnl_value: Decimal
    unrealized_pnl_pct: Decimal | None = None
    portfolio_change_24h_pct: Decimal | None = None
    positions: list[PositionMetricsRead] = Field(default_factory=list)
    top_gainers: list[str] = Field(default_factory=list)
    top_losers: list[str] = Field(default_factory=list)
    action_summary: str
    concentration_warnings: list[str] = Field(default_factory=list)


class PortfolioMonitorResult(ORMBaseModel):
    portfolio_id: UUID
    portfolio_name: str
    signal_count: int
    evaluation: PortfolioEvaluationRead
    generated_signals: list[SignalRead] = Field(default_factory=list)


class BulkMonitorResponse(ORMBaseModel):
    run_reason: str
    completed_at: datetime
    results: list[PortfolioMonitorResult] = Field(default_factory=list)


class DigestResult(ORMBaseModel):
    portfolio_id: UUID
    digest: DailyDigestRead
    sent_to_telegram: bool
