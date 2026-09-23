from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.db.models import SignalSeverity, SignalType, StrategyProfileCode
from app.providers.interfaces import MarketQuote


DECIMAL_ZERO = Decimal("0")
DECIMAL_HUNDRED = Decimal("100")


@dataclass(slots=True)
class PositionMetrics:
    position_id: UUID
    asset_id: UUID
    symbol: str
    asset_name: str | None
    quantity: Decimal
    average_entry_price: Decimal
    cost_basis: Decimal
    current_price: Decimal
    position_value: Decimal
    unrealized_pnl_value: Decimal
    unrealized_pnl_pct: Decimal | None
    weight_pct: Decimal | None
    move_15m_pct: Decimal | None
    move_1h_pct: Decimal | None
    move_4h_pct: Decimal | None
    move_24h_pct: Decimal | None
    peak_price_48h: Decimal | None
    drop_from_peak_pct: Decimal | None
    market_cap_usd: Decimal | None
    volume_24h_usd: Decimal | None
    provider: str
    quote: MarketQuote


@dataclass(slots=True)
class PortfolioMetrics:
    portfolio_id: UUID
    portfolio_name: str
    strategy_profile_code: StrategyProfileCode
    evaluated_at: datetime
    total_value: Decimal
    total_cost_basis: Decimal
    unrealized_pnl_value: Decimal
    unrealized_pnl_pct: Decimal | None
    portfolio_change_24h_pct: Decimal | None
    positions: list[PositionMetrics]
    top_gainers: list[str] = field(default_factory=list)
    top_losers: list[str] = field(default_factory=list)
    concentration_warnings: list[str] = field(default_factory=list)
    action_summary: str = "No immediate action."


@dataclass(slots=True)
class SignalCandidate:
    signal_type: SignalType
    severity: SignalSeverity
    confidence_score: Decimal
    title: str
    message: str
    action_idea: str
    reasoning: str
    risk_note: str
    explanation: str
    event_key: str | None
    cooldown_minutes: int
    asset_id: UUID | None = None
    position_id: UUID | None = None
    metrics_json: dict[str, Any] = field(default_factory=dict)
