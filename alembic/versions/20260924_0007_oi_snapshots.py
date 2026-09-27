"""Store self-recorded open interest observations aligned to 15m close boundaries."""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0007_oi_snapshots"
down_revision = "20260923_0006_exchange"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "open_interest_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_interest", sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column(
            "open_interest_notional", sa.Numeric(precision=38, scale=12), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exchange", "symbol", "bucket_at", name="uq_oi_snapshot_bucket"
        ),
    )
    op.create_index(
        "ix_oi_snapshot_bucket_at",
        "open_interest_snapshots",
        ["bucket_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_oi_snapshot_bucket_at", table_name="open_interest_snapshots")
    op.drop_table("open_interest_snapshots")
