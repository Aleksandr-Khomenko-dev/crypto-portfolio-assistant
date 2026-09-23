from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models import (
    AlertChannel,
    AlertStatus,
    SignalSeverity,
    SignalType,
    StrategyProfileCode,
)


class ORMBaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StrategyProfileRead(ORMBaseModel):
    id: UUID
    code: StrategyProfileCode
    name: str
    description: str
    rule_overrides: dict[str, object]


class UserSettingsRead(ORMBaseModel):
    id: UUID
    user_key: str
    timezone: str
    default_quote_currency: str
    telegram_chat_id: str | None = None
    morning_digest_hour: int
    morning_digest_minute: int
    alerts_enabled: bool


class SignalRead(ORMBaseModel):
    id: UUID
    portfolio_id: UUID
    asset_id: UUID | None = None
    position_id: UUID | None = None
    signal_type: SignalType
    severity: SignalSeverity
    confidence_score: Decimal
    title: str
    message: str
    action_idea: str
    reasoning: str
    risk_note: str
    explanation: str
    event_key: str | None = None
    metrics_json: dict[str, object]
    created_at: datetime


class AlertEventRead(ORMBaseModel):
    id: UUID
    signal_id: UUID
    portfolio_id: UUID
    channel: AlertChannel
    destination: str | None = None
    status: AlertStatus
    dedupe_key: str | None = None
    error_message: str | None = None
    delivered_at: datetime | None = None
    created_at: datetime


class DailyDigestRead(ORMBaseModel):
    id: UUID
    portfolio_id: UUID
    content: str
    summary_json: dict[str, object]
    sent_to_telegram: bool
    created_at: datetime
