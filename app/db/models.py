from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
# Register scanner metadata for init_db and Alembic.
from app.db.scanner_models import (  # noqa: F401
    EarlyEventOutcome,
    FastMarketEvent,
    MarketSetup,
    MarketStructureEpisode,
    NewsAlert,
    NewsClassification,
    NewsClusterStatus,
    NewsEntity,
    NewsEventCluster,
    NewsItem,
    NewsSetupLink,
    OpenInterestSnapshot,
    PatternCandidateRecord,
    ScannerRun,
    ScannerSnapshot,
    SetupOutcome,
    TradingViewEvent,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrategyProfileCode(str, Enum):
    MAIN = "main"
    LONG_TERM = "long_term"


class SignalType(str, Enum):
    MORNING_DIGEST = "morning_digest"
    ABNORMAL_RISE = "abnormal_rise"
    ABNORMAL_DROP = "abnormal_drop"
    EXTREME_PUMP = "extreme_pump"
    TAKE_PROFIT = "take_profit"
    PULLBACK = "pullback"
    RISK = "risk"


class SignalSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AlertChannel(str, Enum):
    TELEGRAM = "telegram"
    API = "api"
    DIGEST = "digest"


class AlertStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class TransactionSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class StrategyProfile(Base):
    __tablename__ = "strategy_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[StrategyProfileCode] = mapped_column(
        SAEnum(StrategyProfileCode, name="strategy_profile_code_enum"),
        unique=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rule_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    portfolios: Mapped[list["Portfolio"]] = relationship(back_populates="strategy_profile")


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Brussels", nullable=False)
    default_quote_currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    morning_digest_hour: Mapped[int] = mapped_column(default=8, nullable=False)
    morning_digest_minute: Mapped[int] = mapped_column(default=0, nullable=False)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    strategy_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("strategy_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    morning_digest_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    strategy_profile: Mapped[StrategyProfile] = relationship(back_populates="portfolios")
    positions: Mapped[list["Position"]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="Position.created_at.asc()",
    )
    signals: Mapped[list["Signal"]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="Signal.created_at.desc()",
    )
    digests: Mapped[list["DailyDigest"]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="DailyDigest.created_at.desc()",
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="Transaction.executed_at.desc()",
    )


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("symbol"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    coingecko_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    binance_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bybit_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    positions: Mapped[list["Position"]] = relationship(back_populates="asset")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="asset")
    price_snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="PriceSnapshot.captured_at.desc()",
    )
    signals: Mapped[list["Signal"]] = relationship(back_populates="asset")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("portfolio_id", "asset_id", name="uq_portfolio_asset"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    average_entry_price: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    cost_basis: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    target_weight_pct: Mapped[Any | None] = mapped_column(Numeric(10, 4), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_alert_take_profit_usd: Mapped[Any | None] = mapped_column(Numeric(30, 12), nullable=True)
    price_alert_stop_loss_usd: Mapped[Any | None] = mapped_column(Numeric(30, 12), nullable=True)
    last_manual_update_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="positions")
    asset: Mapped[Asset] = relationship(back_populates="positions")
    signals: Mapped[list["Signal"]] = relationship(back_populates="position")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="position")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    side: Mapped[TransactionSide] = mapped_column(
        SAEnum(TransactionSide, name="transaction_side_enum"),
        nullable=False,
    )
    quantity: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    unit_price: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    fee_amount: Mapped[Any | None] = mapped_column(Numeric(30, 12), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="transactions")
    asset: Mapped[Asset] = relationship(back_populates="transactions")
    position: Mapped[Position | None] = relationship(back_populates="transactions")


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    price_usd: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    market_cap_usd: Mapped[Any | None] = mapped_column(Numeric(30, 2), nullable=True)
    volume_24h_usd: Mapped[Any | None] = mapped_column(Numeric(30, 2), nullable=True)
    change_1h_pct: Mapped[Any | None] = mapped_column(Numeric(12, 6), nullable=True)
    change_24h_pct: Mapped[Any | None] = mapped_column(Numeric(12, 6), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    asset: Mapped[Asset] = relationship(back_populates="price_snapshots")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    signal_type: Mapped[SignalType] = mapped_column(
        SAEnum(SignalType, name="signal_type_enum"),
        nullable=False,
    )
    severity: Mapped[SignalSeverity] = mapped_column(
        SAEnum(SignalSeverity, name="signal_severity_enum"),
        nullable=False,
    )
    confidence_score: Mapped[Any] = mapped_column(Numeric(5, 2), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    action_idea: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    risk_note: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    event_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="signals")
    asset: Mapped[Asset | None] = relationship(back_populates="signals")
    position: Mapped[Position | None] = relationship(back_populates="signals")
    alert_events: Mapped[list["AlertEvent"]] = relationship(
        back_populates="signal",
        cascade="all, delete-orphan",
        order_by="AlertEvent.created_at.desc()",
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("signals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[AlertChannel] = mapped_column(
        SAEnum(AlertChannel, name="alert_channel_enum"),
        nullable=False,
    )
    destination: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[AlertStatus] = mapped_column(
        SAEnum(AlertStatus, name="alert_status_enum"),
        default=AlertStatus.PENDING,
        nullable=False,
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    signal: Mapped[Signal] = relationship(back_populates="alert_events")


class DailyDigest(Base):
    __tablename__ = "daily_digests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    sent_to_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="digests")


Index("ix_price_snapshot_asset_captured", PriceSnapshot.asset_id, PriceSnapshot.captured_at)
Index("ix_signal_portfolio_created", Signal.portfolio_id, Signal.created_at)
Index("ix_digest_portfolio_created", DailyDigest.portfolio_id, DailyDigest.created_at)
