"""Add setup outcome research tracking and scanner run telemetry."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0005_setup_outcomes"
down_revision = "20260923_0004_scanner"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(precision=30, scale=12)


def upgrade() -> None:
    with op.batch_alter_table("scanner_runs") as batch:
        batch.add_column(
            sa.Column("telemetry", sa.JSON(), nullable=False, server_default="{}")
        )
    op.create_table(
        "setup_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("market_setup_id", sa.Uuid(), nullable=False),
        sa.Column("ready_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("timeframe", sa.String(length=5), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_reference_price", PRICE, nullable=False),
        sa.Column("invalidation_price", PRICE, nullable=False),
        sa.Column("initial_risk_distance", PRICE, nullable=False),
        sa.Column("score_at_entry", sa.Integer(), nullable=False),
        sa.Column("score_breakdown", sa.JSON(), nullable=False),
        sa.Column("market_regime", sa.String(length=32), nullable=False),
        sa.Column("structure_regime", sa.String(length=16), nullable=False),
        sa.Column("target_0_5r_price", PRICE, nullable=False),
        sa.Column("target_1r_price", PRICE, nullable=False),
        sa.Column("target_1_5r_price", PRICE, nullable=False),
        sa.Column("target_2r_price", PRICE, nullable=False),
        sa.Column("hit_0_5r", sa.Boolean(), nullable=False),
        sa.Column("hit_1r", sa.Boolean(), nullable=False),
        sa.Column("hit_1_5r", sa.Boolean(), nullable=False),
        sa.Column("hit_2r", sa.Boolean(), nullable=False),
        sa.Column("hit_minus_1r", sa.Boolean(), nullable=False),
        sa.Column("first_0_5r", sa.String(length=20), nullable=False),
        sa.Column("first_1r", sa.String(length=20), nullable=False),
        sa.Column("first_1_5r", sa.String(length=20), nullable=False),
        sa.Column("first_2r", sa.String(length=20), nullable=False),
        sa.Column("first_event", sa.String(length=24), nullable=True),
        sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_favorable_price", PRICE, nullable=False),
        sa.Column("max_adverse_price", PRICE, nullable=False),
        sa.Column("max_favorable_excursion_r", sa.Float(), nullable=False),
        sa.Column("max_adverse_excursion_r", sa.Float(), nullable=False),
        sa.Column("bars_to_0_5r", sa.Integer(), nullable=True),
        sa.Column("bars_to_1r", sa.Integer(), nullable=True),
        sa.Column("bars_to_1_5r", sa.Integer(), nullable=True),
        sa.Column("bars_to_2r", sa.Integer(), nullable=True),
        sa.Column("bars_to_invalidation", sa.Integer(), nullable=True),
        sa.Column("bars_processed", sa.Integer(), nullable=False),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ambiguity_resolution", sa.String(length=24), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["market_setup_id"], ["market_setups.id"]),
        sa.ForeignKeyConstraint(["ready_snapshot_id"], ["scanner_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_setup_id"),
    )
    op.create_index(
        "ix_setup_outcome_status_cursor",
        "setup_outcomes",
        ["outcome_status", "last_processed_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_setup_outcomes_ready_at"), "setup_outcomes", ["ready_at"], unique=False
    )
    op.create_index(
        op.f("ix_setup_outcomes_score_at_entry"),
        "setup_outcomes",
        ["score_at_entry"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_setup_outcomes_score_at_entry"), table_name="setup_outcomes")
    op.drop_index(op.f("ix_setup_outcomes_ready_at"), table_name="setup_outcomes")
    op.drop_index("ix_setup_outcome_status_cursor", table_name="setup_outcomes")
    op.drop_table("setup_outcomes")
    with op.batch_alter_table("scanner_runs") as batch:
        batch.drop_column("telemetry")
