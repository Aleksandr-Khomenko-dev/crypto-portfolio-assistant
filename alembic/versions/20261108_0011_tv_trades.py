"""TradingView ICT v4.2 trades: raw webhooks, trade plans, events and deliveries.

New tables only; no existing table or row is changed.

Revision ID: 20261108_0011_tv_trades
Revises: 20260924_0010_early_events
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261108_0011_tv_trades"
down_revision = "20260924_0010_early_events"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(30, 12)


def upgrade() -> None:
    op.create_table(
        "raw_webhook",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("secret_ok", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=True),
        sa.Column("plan_id", sa.String(length=120), nullable=True),
        sa.Column("event", sa.String(length=24), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_raw_webhook_status", "raw_webhook", ["status", "id"], unique=False
    )
    op.create_table(
        "trade",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=60), nullable=True),
        sa.Column("ticker", sa.String(length=40), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=True),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("entry_type", sa.String(length=16), nullable=True),
        sa.Column("entry", PRICE, nullable=True),
        sa.Column("sl", PRICE, nullable=True),
        sa.Column("tp1", PRICE, nullable=True),
        sa.Column("tp2", PRICE, nullable=True),
        sa.Column("tp3", PRICE, nullable=True),
        sa.Column("tp2_name", sa.String(length=60), nullable=True),
        sa.Column("tp3_name", sa.String(length=60), nullable=True),
        sa.Column("units", PRICE, nullable=True),
        sa.Column("risk_usd", PRICE, nullable=True),
        sa.Column("leverage", PRICE, nullable=True),
        sa.Column("window", sa.String(length=60), nullable=True),
        sa.Column("checklist", sa.String(length=300), nullable=True),
        sa.Column("bias_main", sa.Integer(), nullable=True),
        sa.Column("bias_4h", sa.Integer(), nullable=True),
        sa.Column("day_range_pct", sa.Float(), nullable=True),
        sa.Column("valid_hours", sa.Float(), nullable=True),
        sa.Column("born_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("notify_reason", sa.String(length=60), nullable=True),
        sa.Column("result_r", sa.Float(), nullable=False),
        sa.Column("scanner_agreement", sa.String(length=16), nullable=True),
        sa.Column("news_nearby", sa.Boolean(), nullable=True),
        sa.Column("late_minutes", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id"),
    )
    op.create_index("ix_trade_ticker", "trade", ["ticker"], unique=False)
    op.create_index("ix_trade_status", "trade", ["status"], unique=False)
    op.create_table(
        "trade_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=120), nullable=False),
        sa.Column("event", sa.String(length=24), nullable=False),
        sa.Column("raw_webhook_id", sa.Integer(), nullable=True),
        sa.Column("price", PRICE, nullable=True),
        sa.Column("exit_price", PRICE, nullable=True),
        sa.Column("result_r", sa.Float(), nullable=True),
        sa.Column("bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified", sa.Boolean(), nullable=False),
        sa.Column("notify_reason", sa.String(length=60), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["raw_webhook_id"], ["raw_webhook.id"]),
        sa.ForeignKeyConstraint(["trade_id"], ["trade.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "event", name="uq_trade_event_plan_event"),
    )
    op.create_index(
        "ix_trade_event_trade_id", "trade_event", ["trade_id"], unique=False
    )
    op.create_table(
        "delivery",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_event_id", sa.Integer(), nullable=False),
        sa.Column("audience", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("reply_to", sa.Integer(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["trade_event_id"], ["trade_event.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delivery_trade_event_id", "delivery", ["trade_event_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_trade_event_id", table_name="delivery")
    op.drop_table("delivery")
    op.drop_index("ix_trade_event_trade_id", table_name="trade_event")
    op.drop_table("trade_event")
    op.drop_index("ix_trade_status", table_name="trade")
    op.drop_index("ix_trade_ticker", table_name="trade")
    op.drop_table("trade")
    op.drop_index("ix_raw_webhook_status", table_name="raw_webhook")
    op.drop_table("raw_webhook")
