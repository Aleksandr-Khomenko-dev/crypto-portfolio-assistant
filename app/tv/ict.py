"""Parsing and checks for ICT v4.2 webhook JSON (pure: no network, no database)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# What each indicator event means for the chat.
ENTRY_EVENTS = {"ENTRY", "PLAN_NEW"}  # the main message of a plan
FOLLOW_UPS = {
    "TP1",
    "TP2",
    "STOP",
    "REST_CLOSED",
    "EARLY_EXIT",
    "TIME_EXIT",
    "FILLED",
}  # replies under the entry message
SILENT = {"ZONE_WAIT", "CANCELLED"}  # stored only (visible via /watch)
CLOSING = {"TP2", "STOP", "REST_CLOSED", "EARLY_EXIT", "TIME_EXIT"}
KNOWN_EVENTS = ENTRY_EVENTS | FOLLOW_UPS | SILENT

Format = Literal["v42", "legacy"]


class IctSignal(BaseModel):
    """ICT v4.2 alert() JSON. Unknown fields are ignored and every field except
    event, planId, ticker and side may be missing or null (the format grows)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event: str = Field(min_length=1, max_length=24)
    plan_id: str = Field(alias="planId", min_length=1, max_length=120)
    ticker: str = Field(min_length=1, max_length=40)
    side: Literal["BUY", "SELL"]
    symbol: str | None = Field(default=None, max_length=60)
    tf: str | None = Field(default=None, max_length=8)
    model: str | None = Field(default=None, max_length=60)
    score: int | None = None
    entry_type: str | None = Field(default=None, alias="entryType", max_length=16)
    entry: Decimal | None = None
    sl: Decimal | None = None
    tp1: Decimal | None = None
    tp2: Decimal | None = None
    tp3: Decimal | None = None
    tp2_name: str | None = Field(default=None, alias="tp2Name", max_length=60)
    tp3_name: str | None = Field(default=None, alias="tp3Name", max_length=60)
    price: Decimal | None = None
    result_r: float | None = Field(default=None, alias="resultR")
    exit_price: Decimal | None = Field(default=None, alias="exitPrice")
    units: Decimal | None = None
    risk_usd: Decimal | None = Field(default=None, alias="riskUsd")
    leverage: Decimal | None = None
    bar_time: int | None = Field(default=None, alias="barTime")  # bar OPEN, ms UTC
    born_time: int | None = Field(default=None, alias="bornTime")
    valid_hours: float | None = Field(default=None, alias="validHours")
    bias_main: int | None = Field(default=None, alias="biasMain")
    bias_4h: int | None = Field(default=None, alias="bias4h")
    day_range_pct: float | None = Field(default=None, alias="dayRangePct")
    window: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=300)
    checklist: str | None = Field(default=None, max_length=300)

    @field_validator("event", "side", mode="before")
    @classmethod
    def upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("ticker", mode="before")
    @classmethod
    def plain_ticker(cls, value: Any) -> Any:
        return normalize_ticker(value) if isinstance(value, str) else value

    @property
    def direction(self) -> str:  # the scanner's vocabulary
        return "LONG" if self.side == "BUY" else "SHORT"

    @staticmethod
    def time(ms: int | None) -> datetime | None:
        return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


def normalize_ticker(raw: str) -> str:
    """OANDA:XAUUSD -> XAUUSD, BINGX:ETHUSDT.P -> ETHUSDT."""
    ticker = raw.strip().upper().split(":")[-1]
    return ticker.removesuffix(".P")


def decode(body: str, exact: bool = False) -> dict[str, Any] | None:
    """The JSON object, or None. exact=True keeps prices as Decimal (no float
    rounding, trailing zeros kept); the default suits JSON storage."""
    try:
        data = json.loads(body, parse_float=Decimal) if exact else json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def detect_format(data: dict[str, Any] | None, legacy_enabled: bool) -> Format | None:
    """planId -> ICT v4.2; else the legacy QS PRO Gold format when it is enabled."""
    if data is None:
        return None
    if "planId" in data:
        return "v42"
    if legacy_enabled and "event_id" in data and "secret" in data:
        return "legacy"
    return None


def parse(data: dict[str, Any]) -> IctSignal:
    return IctSignal.model_validate(data)


def validation_error(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}"[:300]


def check_levels(signal: IctSignal) -> str | None:
    """None when the entry plan is coherent; otherwise why it is rejected.
    BUY: sl < entry < tp1. SELL: sl > entry > tp1. Every price > 0."""
    entry, sl, tp1 = signal.entry, signal.sl, signal.tp1
    if entry is None or sl is None or tp1 is None:
        return "entry, sl and tp1 are required for an entry"
    prices = [p for p in (entry, sl, tp1, signal.tp2, signal.tp3) if p is not None]
    if any(not p.is_finite() or p <= 0 for p in prices):
        return "prices must be positive numbers"
    if signal.side == "BUY" and not sl < entry < tp1:
        return "BUY requires sl < entry < tp1"
    if signal.side == "SELL" and not sl > entry > tp1:
        return "SELL requires sl > entry > tp1"
    return None


def timeframe_seconds(tf: str | None) -> int | None:
    """TradingView interval strings: "5" (minutes), "60", "240", "D"/"1D", "W",
    "M", "30S". None when unknown (freshness then cannot be judged)."""
    if not tf:
        return None
    tf = tf.strip().upper()
    unit = {"S": 1, "D": 86400, "W": 604800, "M": 2592000}
    if tf[-1] in unit:
        count = tf[:-1] or "1"
        return int(count) * unit[tf[-1]] if count.isdigit() else None
    return int(tf) * 60 if tf.isdigit() else None


def timeframe_label(tf: str | None) -> str:
    seconds = timeframe_seconds(tf)
    if seconds is None:
        return tf or "?"
    for size, suffix in ((604800, "W"), (86400, "D"), (3600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def candle_close(signal: IctSignal) -> datetime | None:
    """barTime is the bar OPEN; the reaction candle closed one interval later."""
    opened, seconds = signal.time(signal.bar_time), timeframe_seconds(signal.tf)
    if opened is None or seconds is None:
        return None
    return opened + timedelta(seconds=seconds)


def late_minutes(signal: IctSignal, received_at: datetime) -> float | None:
    closed = candle_close(signal)
    if closed is None:
        return None
    return max(0.0, (received_at - closed).total_seconds() / 60)


def symbol_allowed(ticker: str, allowed: str) -> bool:
    wanted = {normalize_ticker(s) for s in allowed.split(",") if s.strip()}
    return not wanted or normalize_ticker(ticker) in wanted


def lookup(table: str, ticker: str) -> str | None:
    """ "XAUUSD=100,ETHUSDT=1" style settings -> value for one ticker."""
    for item in table.split(","):
        key, _, value = item.partition("=")
        if normalize_ticker(key) == normalize_ticker(ticker) and value.strip():
            return value.strip()
    return None
