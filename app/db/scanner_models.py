from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
)
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
    telemetry: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScannerSnapshot(Base):
    __tablename__ = "scanner_snapshots"
    __table_args__ = (
        Index("ix_scanner_snapshot_symbol_created", "symbol", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scanner_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    # Source exchange of every metric in the snapshot; rows before BingX were Binance.
    exchange: Mapped[str] = mapped_column(
        String(16), default="BINANCE", server_default="BINANCE"
    )
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
    # Episodes are per exchange: a BingX result never updates a Binance episode.
    exchange: Mapped[str] = mapped_column(
        String(16), default="BINANCE", server_default="BINANCE"
    )
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


PRICE = Numeric(30, 12)


class SetupOutcome(Base):
    """Research outcome of one setup episode, measured from its first READY closed bar.

    Symbol, direction and episode creation time live on MarketSetup. Everything frozen
    at READY (entry, invalidation, targets, score) is stored here and never recomputed.
    """

    __tablename__ = "setup_outcomes"
    __table_args__ = (
        Index("ix_setup_outcome_status_cursor", "outcome_status", "last_processed_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    market_setup_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_setups.id"), unique=True
    )
    ready_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scanner_snapshots.id")
    )
    timeframe: Mapped[str] = mapped_column(String(5))
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_reference_price: Mapped[Decimal] = mapped_column(PRICE)
    invalidation_price: Mapped[Decimal] = mapped_column(PRICE)
    initial_risk_distance: Mapped[Decimal] = mapped_column(PRICE)
    score_at_entry: Mapped[int] = mapped_column(index=True)
    score_breakdown: Mapped[dict[str, int]] = mapped_column(JSON)
    market_regime: Mapped[str] = mapped_column(String(32))
    structure_regime: Mapped[str] = mapped_column(String(16))
    target_0_5r_price: Mapped[Decimal] = mapped_column(PRICE)
    target_1r_price: Mapped[Decimal] = mapped_column(PRICE)
    target_1_5r_price: Mapped[Decimal] = mapped_column(PRICE)
    target_2r_price: Mapped[Decimal] = mapped_column(PRICE)
    hit_0_5r: Mapped[bool] = mapped_column(Boolean, default=False)
    hit_1r: Mapped[bool] = mapped_column(Boolean, default=False)
    hit_1_5r: Mapped[bool] = mapped_column(Boolean, default=False)
    hit_2r: Mapped[bool] = mapped_column(Boolean, default=False)
    hit_minus_1r: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per target: PENDING, TARGET_FIRST, INVALIDATION_FIRST, AMBIGUOUS or UNRESOLVED.
    first_0_5r: Mapped[str] = mapped_column(String(20), default="PENDING")
    first_1r: Mapped[str] = mapped_column(String(20), default="PENDING")
    first_1_5r: Mapped[str] = mapped_column(String(20), default="PENDING")
    first_2r: Mapped[str] = mapped_column(String(20), default="PENDING")
    first_event: Mapped[str | None] = mapped_column(String(24))
    first_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_favorable_price: Mapped[Decimal] = mapped_column(PRICE)
    max_adverse_price: Mapped[Decimal] = mapped_column(PRICE)
    max_favorable_excursion_r: Mapped[float] = mapped_column(Float, default=0.0)
    max_adverse_excursion_r: Mapped[float] = mapped_column(Float, default=0.0)
    bars_to_0_5r: Mapped[int | None]
    bars_to_1r: Mapped[int | None]
    bars_to_1_5r: Mapped[int | None]
    bars_to_2r: Mapped[int | None]
    bars_to_invalidation: Mapped[int | None]
    bars_processed: Mapped[int] = mapped_column(default=0)
    last_processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ambiguity_resolution: Mapped[str | None] = mapped_column(String(24))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_status: Mapped[str] = mapped_column(String(24), default="TRACKING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
