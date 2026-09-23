from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.scanner_models import TradingViewEvent
from app.db.session import get_db
from app.scanner.domain import Direction, Timeframe

Database = Annotated[Session, Depends(get_db)]
Configuration = Annotated[Settings, Depends(get_settings)]

router = APIRouter(tags=["webhooks"])


class TradingViewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    secret: SecretStr
    event_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w:.-]+$")
    symbol: str = Field(pattern=r"^[A-Z0-9_]{2,40}$")
    timeframe: Timeframe
    event: Literal[
        "BOS", "CHoCH", "RETEST", "FVG", "FOOTPRINT_DELTA", "POC", "AGGRESSION"
    ]
    direction: Direction
    timestamp: AwareDatetime
    price: Decimal = Field(gt=0)
    value: Decimal | None = None


class WebhookRead(BaseModel):
    accepted: bool
    duplicate: bool
    used_in_scoring: bool = False


@router.post("/webhooks/tradingview", response_model=WebhookRead)
def tradingview(
    payload: TradingViewPayload, db: Database, settings: Configuration
) -> WebhookRead:
    if not settings.tradingview_webhook_enabled:
        raise HTTPException(404, "Webhook disabled")
    if not secrets.compare_digest(
        payload.secret.get_secret_value(), settings.tradingview_webhook_secret or ""
    ):
        raise HTTPException(401, "Invalid webhook secret")
    now = datetime.now(UTC)
    if abs((now - payload.timestamp).total_seconds()) > 300:
        raise HTTPException(422, "Event timestamp must be within five minutes")
    if db.get(TradingViewEvent, payload.event_id):
        return WebhookRead(accepted=True, duplicate=True)
    db.add(
        TradingViewEvent(
            id=payload.event_id,
            received_at=now,
            data=payload.model_dump(mode="json", exclude={"secret"}),
        )
    )
    db.commit()
    return WebhookRead(accepted=True, duplicate=False)
