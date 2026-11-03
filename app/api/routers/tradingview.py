from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.scanner_models import TradingViewEvent
from app.db.session import get_db
from app.macro import GOLD_SYMBOLS
from app.scanner.domain import Direction
from app.scanner.universe import symbols as parse_symbols

Database = Annotated[Session, Depends(get_db)]
Configuration = Annotated[Settings, Depends(get_settings)]

router = APIRouter(tags=["webhooks"])
logger = logging.getLogger(__name__)


WebhookTimeframe = Literal["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"]


class TradingViewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    secret: SecretStr
    event_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w:.-]+$")
    symbol: str = Field(pattern=r"^[A-Z0-9_]{2,40}$")
    timeframe: WebhookTimeframe
    event: Literal[
        "BOS",
        "CHoCH",
        "RETEST",
        "FVG",
        "FOOTPRINT_DELTA",
        "POC",
        "AGGRESSION",
        "SIGNAL",  # a complete QS PRO Gold trade plan (fields below)
    ]
    direction: Direction
    timestamp: AwareDatetime
    price: Decimal = Field(gt=0)
    value: Decimal | None = None
    grade: Literal["A+", "STANDARD"] | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    stop: Decimal | None = Field(default=None, gt=0)
    tp1: Decimal | None = Field(default=None, gt=0)
    tp2: Decimal | None = Field(default=None, gt=0)
    tp3: Decimal | None = Field(default=None, gt=0)
    lots: Decimal | None = Field(default=None, ge=0)
    risk_usd: Decimal | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=300)
    source: str | None = Field(default=None, max_length=60)


class WebhookRead(BaseModel):
    accepted: bool
    duplicate: bool
    used_in_scoring: bool = False
    forwarded: bool = False  # queued for Telegram (SIGNAL events only)


def bot_symbol(tradingview_symbol: str, settings: Settings) -> str:
    """XAUUSD / GOLD / XAUUSDT_P -> the scanner's gold symbol; BTCUSDT_P -> BTCUSDT."""
    symbol = tradingview_symbol.upper().removesuffix("_P")
    if "XAU" in symbol or "GOLD" in symbol:
        whitelist = parse_symbols(settings.scanner_whitelist)
        gold = sorted(s for s in GOLD_SYMBOLS if not whitelist or s in whitelist)
        return gold[0] if gold else min(GOLD_SYMBOLS)
    return symbol if symbol.endswith("USDT") else symbol + "USDT"


def forward_text(db: Session, payload: TradingViewPayload, settings: Settings) -> str:
    from app.macro.calendar import EconomicCalendar
    from app.scanner.repository import ScannerRepository
    from app.telegram.tradingview import format_tradingview_signal

    now = datetime.now(UTC)
    symbol = bot_symbol(payload.symbol, settings)
    results = ScannerRepository(db).latest_results(symbol=symbol, limit=1)
    upcoming = (
        # The runner keeps the calendar cache file fresh; read it, no network here.
        EconomicCalendar(settings).upcoming(now, timedelta(days=7))
        if settings.calendar_enabled
        else []
    )
    return format_tradingview_signal(
        payload,
        symbol,
        results[0] if results else None,
        upcoming[0] if upcoming else None,
        settings,
        now,
    )


async def send_forward(settings: Settings, text: str) -> None:
    from app.services.telegram_service import TelegramService

    try:
        await TelegramService(settings).send_message(
            settings.scanner_telegram_chat_id or "", text
        )
    except Exception as exc:  # noqa: BLE001 - the event is already stored
        logger.warning("TradingView forward failed error=%s", type(exc).__name__)


@router.post("/webhooks/tradingview", response_model=WebhookRead)
def tradingview(
    payload: TradingViewPayload,
    db: Database,
    settings: Configuration,
    background: BackgroundTasks,
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
    forward = (
        payload.event == "SIGNAL"
        and settings.tradingview_forward_telegram
        and bool(settings.telegram_bot_token)
        and bool(settings.scanner_telegram_chat_id)
    )
    if forward:
        background.add_task(send_forward, settings, forward_text(db, payload, settings))
    return WebhookRead(accepted=True, duplicate=False, forwarded=forward)
