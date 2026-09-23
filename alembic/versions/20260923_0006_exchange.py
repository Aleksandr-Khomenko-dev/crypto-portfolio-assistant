"""Record the source exchange on scanner snapshots and setup episodes.

Existing rows were produced by the Binance adapter, so they default to BINANCE.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0006_exchange"
down_revision = "20260923_0005_setup_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("scanner_snapshots", "market_setups"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "exchange",
                    sa.String(length=16),
                    nullable=False,
                    server_default="BINANCE",
                )
            )


def downgrade() -> None:
    for table in ("market_setups", "scanner_snapshots"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("exchange")
