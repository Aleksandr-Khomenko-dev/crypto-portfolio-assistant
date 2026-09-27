"""Fast market watcher events (early-warning research log, not a trading score).

Revision ID: 20260924_0009_fast_events
Revises: 20260924_0008_news
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260924_0009_fast_events"
down_revision = "20260924_0008_news"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fast_market_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("direction", sa.String(length=5), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("window", sa.String(length=4), nullable=False),
        sa.Column("change_pct", sa.Float(), nullable=False),
        sa.Column("threshold_pct", sa.Float(), nullable=False),
        sa.Column("sigma_pct", sa.Float(), nullable=False),
        sa.Column("volume_ratio", sa.Float(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("technical_score", sa.Integer(), nullable=True),
        sa.Column("setup_state", sa.String(length=32), nullable=True),
        sa.Column("market_setup_id", sa.Uuid(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent", sa.Boolean(), nullable=False),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["market_setup_id"], ["market_setups.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fast_event_symbol_detected",
        "fast_market_events",
        ["symbol", "direction", "detected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fast_event_symbol_detected", table_name="fast_market_events")
    op.drop_table("fast_market_events")
