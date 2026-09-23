"""Add EXTREME_PUMP and PULLBACK to signal_type_enum.

Revision ID: 20260404_0002
Revises: 20260329_0001
Create Date: 2026-04-04 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260404_0002"
down_revision = "20260329_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite stores these enums as unconstrained strings.
    # PostgreSQL requires ALTER TYPE to add enum values
    op.execute("ALTER TYPE signal_type_enum ADD VALUE IF NOT EXISTS 'EXTREME_PUMP'")
    op.execute("ALTER TYPE signal_type_enum ADD VALUE IF NOT EXISTS 'PULLBACK'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the type.
    # Downgrade is intentionally a no-op to avoid data loss.
    pass
