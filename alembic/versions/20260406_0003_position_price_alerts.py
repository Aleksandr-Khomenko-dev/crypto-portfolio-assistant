"""Add optional take-profit / stop-loss price alerts on positions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260406_0003_price_alerts"
down_revision = "20260404_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("price_alert_take_profit_usd", sa.Numeric(30, 12), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("price_alert_stop_loss_usd", sa.Numeric(30, 12), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "price_alert_stop_loss_usd")
    op.drop_column("positions", "price_alert_take_profit_usd")
