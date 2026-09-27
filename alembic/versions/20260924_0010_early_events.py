"""Early-event layer: event typing on fast events, breakout episodes, pattern
candidates and early-event outcomes (research only; never a trading score).

Existing fast_market_events rows are preserved (batch copy on SQLite); only new
nullable columns are added and fast-only columns become nullable.

Revision ID: 20260924_0010_early_events
Revises: 20260924_0009_fast_events
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260924_0010_early_events"
down_revision = "20260924_0009_fast_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_structure_episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("direction", sa.String(length=5), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("zone", sa.JSON(), nullable=False),
        sa.Column("zone_timeframe", sa.String(length=8), nullable=False),
        sa.Column("zone_type", sa.String(length=16), nullable=False),
        sa.Column("zone_lower", sa.Float(), nullable=False),
        sa.Column("zone_upper", sa.Float(), nullable=False),
        sa.Column("zone_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("touch_count", sa.Integer(), nullable=False),
        sa.Column("level", sa.Float(), nullable=False),
        sa.Column("break_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("break_price", sa.Float(), nullable=False),
        sa.Column("origin_price", sa.Float(), nullable=True),
        sa.Column("max_extension_pct", sa.Float(), nullable=False),
        sa.Column("max_extension_atr", sa.Float(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("retest_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retest_extreme", sa.Float(), nullable=True),
        sa.Column("momentum_sent", sa.Boolean(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_structure_episode_phase",
        "market_structure_episodes",
        ["symbol", "phase"],
        unique=False,
    )
    op.create_table(
        "pattern_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("pattern_key", sa.String(length=120), nullable=False),
        sa.Column("pattern_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=5), nullable=False),
        sa.Column("source_timeframe", sa.String(length=8), nullable=False),
        sa.Column("formation_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("boundary_level", sa.Float(), nullable=False),
        sa.Column("secondary_boundary", sa.Float(), nullable=True),
        sa.Column("touch_count", sa.Integer(), nullable=False),
        sa.Column("compression_ratio", sa.Float(), nullable=True),
        sa.Column("distance_to_trigger_atr", sa.Float(), nullable=False),
        sa.Column("formation_strength", sa.Integer(), nullable=False),
        sa.Column("invalidation_condition", sa.String(length=120), nullable=False),
        sa.Column("invalidation_level", sa.Float(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pattern_candidate_key",
        "pattern_candidates",
        ["symbol", "pattern_key"],
        unique=False,
    )
    with op.batch_alter_table("fast_market_events") as batch:
        batch.alter_column("window", existing_type=sa.String(length=4), nullable=True)
        for name in ("change_pct", "threshold_pct", "sigma_pct"):
            batch.alter_column(name, existing_type=sa.Float(), nullable=True)
        batch.add_column(sa.Column("event_type", sa.String(length=24), nullable=True))
        batch.add_column(sa.Column("strength", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("source_timeframe", sa.String(length=8), nullable=True)
        )
        batch.add_column(sa.Column("zone", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("metrics", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("episode_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_fast_event_episode",
            "market_structure_episodes",
            ["episode_id"],
            ["id"],
        )
        batch.create_index(
            "ix_fast_market_events_event_type", ["event_type"], unique=False
        )
    op.create_table(
        "early_event_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("direction", sa.String(length=5), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("invalidation", sa.Float(), nullable=True),
        sa.Column("r_value", sa.Float(), nullable=True),
        sa.Column("mfe_pct", sa.Float(), nullable=True),
        sa.Column("mae_pct", sa.Float(), nullable=True),
        sa.Column("mfe_r", sa.Float(), nullable=True),
        sa.Column("mae_r", sa.Float(), nullable=True),
        sa.Column("first_1r", sa.String(length=20), nullable=False),
        sa.Column("first_2r", sa.String(length=20), nullable=False),
        sa.Column("minutes_to_1r", sa.Float(), nullable=True),
        sa.Column("minutes_to_invalidation", sa.Float(), nullable=True),
        sa.Column("continuation", sa.Boolean(), nullable=True),
        sa.Column("next_event_type", sa.String(length=24), nullable=True),
        sa.Column("minutes_to_next_event", sa.Float(), nullable=True),
        sa.Column("bars_used", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["fast_market_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index(
        "ix_early_event_outcomes_event_type",
        "early_event_outcomes",
        ["event_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_early_event_outcomes_event_type", table_name="early_event_outcomes"
    )
    op.drop_table("early_event_outcomes")
    with op.batch_alter_table("fast_market_events") as batch:
        batch.drop_index("ix_fast_market_events_event_type")
        batch.drop_constraint("fk_fast_event_episode", type_="foreignkey")
        for name in (
            "episode_id",
            "metrics",
            "zone",
            "source_timeframe",
            "strength",
            "event_type",
        ):
            batch.drop_column(name)
    op.drop_index("ix_pattern_candidate_key", table_name="pattern_candidates")
    op.drop_table("pattern_candidates")
    op.drop_index("ix_structure_episode_phase", table_name="market_structure_episodes")
    op.drop_table("market_structure_episodes")
