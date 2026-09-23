from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ScannerRun(Base):
    __tablename__ = "scanner_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    universe_size: Mapped[int] = mapped_column(default=0)
    analyzed: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    high_confluence: Mapped[int] = mapped_column(default=0)
    duration_seconds: Mapped[float] = mapped_column(default=0.0)
    errors: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScannerSnapshot(Base):
    __tablename__ = "scanner_snapshots"
    __table_args__ = (
        Index("ix_scanner_snapshot_symbol_created", "symbol", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    candle_closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    long_score: Mapped[int]
    short_score: Mapped[int]
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class MarketSetup(Base):
    """An episode pins original invalidation/expiry; snapshots preserve each later evaluation."""

    __tablename__ = "market_setups"
    __table_args__ = (
        Index("ix_market_setup_symbol_direction", "symbol", "direction", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scanner_snapshots.id"))
    symbol: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    score: Mapped[int]
    state: Mapped[str] = mapped_column(String(32))
    readiness: Mapped[str] = mapped_column(String(32))
    lifecycle: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    initial_invalidation: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    delivery_error: Mapped[str | None] = mapped_column(String(120))


class TradingViewEvent(Base):
    __tablename__ = "tradingview_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
