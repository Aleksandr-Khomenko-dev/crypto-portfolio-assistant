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
    UniqueConstraint,
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


class OpenInterestSnapshot(Base):
    """A real live OI observation attributed to one 15m close boundary (never filled in).

    One row per (exchange, symbol, bucket); a later observation replaces the stored one
    only if it was taken closer to the boundary.
    """

    __tablename__ = "open_interest_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "exchange", "symbol", "bucket_at", name="uq_oi_snapshot_bucket"
        ),
        Index("ix_oi_snapshot_bucket_at", "bucket_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(40))
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Base-asset quantity (BingX: USDT notional / same-cycle mark price).
    open_interest: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    open_interest_notional: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- News context (read-only research context; never part of the trading score) ---


class NewsItem(Base):
    """A provider item exactly as received. Never updated after insert."""

    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("provider", "provider_item_id", name="uq_news_provider_item"),
        Index("ix_news_items_received_at", "received_at"),
        Index("ix_news_items_payload_hash", "raw_payload_hash"),
    )
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    provider_item_id: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(120))
    source_type: Mapped[int]
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(String(2000), default="")
    url: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    event_type_hint: Mapped[str | None] = mapped_column(String(40))
    raw_payload_hash: Mapped[str] = mapped_column(String(64))
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_event_clusters.id"), index=True
    )
    stages: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)


class NewsEntity(Base):
    __tablename__ = "news_entities"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    news_item_id: Mapped[str] = mapped_column(ForeignKey("news_items.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    match_type: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float)
    matched_text: Mapped[str] = mapped_column(String(120))


class NewsClassification(Base):
    """Append-only: a new rule version adds a row; history is never rewritten."""

    __tablename__ = "news_classifications"
    __table_args__ = (
        UniqueConstraint("news_item_id", "rule_version", name="uq_news_class_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    news_item_id: Mapped[str] = mapped_column(ForeignKey("news_items.id"), index=True)
    rule_version: Mapped[str] = mapped_column(String(40))
    rule: Mapped[str] = mapped_column(String(60))
    event_type: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(32))
    severity: Mapped[int]
    noise: Mapped[bool] = mapped_column(Boolean, default=False)
    noise_reason: Mapped[str | None] = mapped_column(String(40))
    importance_score: Mapped[int]
    importance_level: Mapped[str] = mapped_column(String(10))
    factors: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    verification: Mapped[str] = mapped_column(String(32))
    freshness: Mapped[str] = mapped_column(String(16))
    coverage: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsEventCluster(Base):
    """One real-world event reported by one or more sources."""

    __tablename__ = "news_event_clusters"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(32))
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    canonical_item_id: Mapped[str] = mapped_column(String(160))
    canonical_url: Mapped[str | None] = mapped_column(String(500))
    first_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )


class NewsClusterStatus(Base):
    """Append-only verification/importance history of a cluster."""

    __tablename__ = "news_cluster_status"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news_event_clusters.id"), index=True
    )
    verification: Mapped[str] = mapped_column(String(32))
    importance_score: Mapped[int]
    importance_level: Mapped[str] = mapped_column(String(10))
    source_count: Mapped[int]
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsSetupLink(Base):
    """News known at link time that is relevant to a setup (context only)."""

    __tablename__ = "news_setup_links"
    __table_args__ = (
        UniqueConstraint("cluster_id", "market_setup_id", name="uq_news_setup_link"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news_event_clusters.id"), index=True
    )
    market_setup_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_setups.id"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(40))
    match_type: Mapped[str] = mapped_column(String(20))
    relevance: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(200))
    setup_score_at_link: Mapped[int]
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsAlert(Base):
    """Telegram dispatch log. The unique key is claimed BEFORE sending (at most once)."""

    __tablename__ = "news_alerts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(24))
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("news_event_clusters.id"), index=True
    )
    market_setup_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_setups.id")
    )
    symbol: Mapped[str | None] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(12))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)


class FastMarketEvent(Base):
    """Early-warning fast-move event (not a trading score). Append-only research log;
    also restores per symbol+direction cooldowns after a restart."""

    __tablename__ = "fast_market_events"
    __table_args__ = (
        Index("ix_fast_event_symbol_detected", "symbol", "direction", "detected_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    state: Mapped[str] = mapped_column(String(24))
    decision: Mapped[str] = mapped_column(String(16))
    # Fast-move fields; None for structural early events (zone/break/retest...).
    window: Mapped[str | None] = mapped_column(String(4))
    change_pct: Mapped[float | None] = mapped_column(Float)
    threshold_pct: Mapped[float | None] = mapped_column(Float)
    sigma_pct: Mapped[float | None] = mapped_column(Float)
    volume_ratio: Mapped[float | None] = mapped_column(Float)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    price: Mapped[float] = mapped_column(Float)
    technical_score: Mapped[int | None]
    setup_state: Mapped[str | None] = mapped_column(String(32))
    market_setup_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_setups.id")
    )
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_message_id: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    # Early-event layer (0010). `state` keeps the fast state; `event_type` is the
    # early event (never a scanner state). Strength is a diagnostic, not probability.
    event_type: Mapped[str | None] = mapped_column(String(24), index=True)
    strength: Mapped[int | None]
    source_timeframe: Mapped[str | None] = mapped_column(String(8))
    zone: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_structure_episodes.id")
    )


class MarketStructureEpisode(Base):
    """Breakout episode: FIRST_BREAK -> RETEST_WATCH -> RETEST_CONFIRMED /
    FAILED_BREAKOUT -> COOLED_DOWN. Restores open episodes after a restart."""

    __tablename__ = "market_structure_episodes"
    __table_args__ = (Index("ix_structure_episode_phase", "symbol", "phase"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    kind: Mapped[str] = mapped_column(String(16), default="BREAKOUT")
    zone: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    zone_timeframe: Mapped[str] = mapped_column(String(8))
    zone_type: Mapped[str] = mapped_column(String(16))
    zone_lower: Mapped[float] = mapped_column(Float)
    zone_upper: Mapped[float] = mapped_column(Float)
    zone_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    touch_count: Mapped[int] = mapped_column(default=0)
    level: Mapped[float] = mapped_column(Float)
    break_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    break_price: Mapped[float] = mapped_column(Float)
    origin_price: Mapped[float | None] = mapped_column(Float)
    max_extension_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_extension_atr: Mapped[float] = mapped_column(Float, default=0.0)
    phase: Mapped[str] = mapped_column(String(16))
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    retest_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retest_extreme: Mapped[float | None] = mapped_column(Float)
    momentum_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PatternCandidateRecord(Base):
    """Formation candidate detected BEFORE completion (FORMATION_WATCH)."""

    __tablename__ = "pattern_candidates"
    __table_args__ = (Index("ix_pattern_candidate_key", "symbol", "pattern_key"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(40))
    pattern_key: Mapped[str] = mapped_column(String(120))
    pattern_type: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(5))
    source_timeframe: Mapped[str] = mapped_column(String(8))
    formation_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    boundary_level: Mapped[float] = mapped_column(Float)
    secondary_boundary: Mapped[float | None] = mapped_column(Float)
    touch_count: Mapped[int] = mapped_column(default=0)
    compression_ratio: Mapped[float | None] = mapped_column(Float)
    distance_to_trigger_atr: Mapped[float] = mapped_column(Float)
    formation_strength: Mapped[int] = mapped_column(default=0)
    invalidation_condition: Mapped[str] = mapped_column(String(120))
    invalidation_level: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EarlyEventOutcome(Base):
    """Research outcome of one early event, measured only with LATER candles/events.
    Separate from setup outcomes; never feeds back into detection or scores."""

    __tablename__ = "early_event_outcomes"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fast_market_events.id"), unique=True
    )
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    direction: Mapped[str] = mapped_column(String(5))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon_minutes: Mapped[int]
    entry_price: Mapped[float] = mapped_column(Float)
    invalidation: Mapped[float | None] = mapped_column(Float)
    r_value: Mapped[float | None] = mapped_column(Float)
    mfe_pct: Mapped[float | None] = mapped_column(Float)
    mae_pct: Mapped[float | None] = mapped_column(Float)
    mfe_r: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)
    first_1r: Mapped[str] = mapped_column(String(20))
    first_2r: Mapped[str] = mapped_column(String(20))
    minutes_to_1r: Mapped[float | None] = mapped_column(Float)
    minutes_to_invalidation: Mapped[float | None] = mapped_column(Float)
    continuation: Mapped[bool | None] = mapped_column(Boolean)
    next_event_type: Mapped[str | None] = mapped_column(String(24))
    minutes_to_next_event: Mapped[float | None] = mapped_column(Float)
    bars_used: Mapped[int] = mapped_column(default=0)
