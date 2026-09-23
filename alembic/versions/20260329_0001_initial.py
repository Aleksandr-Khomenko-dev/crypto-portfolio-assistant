"""Initial Portfolio Signal Agent schema.

Revision ID: 20260329_0001
Revises:
Create Date: 2026-03-29 00:00:00
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa


revision = "20260329_0001"
down_revision = None
branch_labels = None
depends_on = None


strategy_profile_code_enum = sa.Enum("MAIN", "LONG_TERM", name="strategy_profile_code_enum")
signal_type_enum = sa.Enum(
    "MORNING_DIGEST",
    "ABNORMAL_RISE",
    "ABNORMAL_DROP",
    "TAKE_PROFIT",
    "RISK",
    name="signal_type_enum",
)
signal_severity_enum = sa.Enum("LOW", "MEDIUM", "HIGH", name="signal_severity_enum")
alert_channel_enum = sa.Enum("TELEGRAM", "API", "DIGEST", name="alert_channel_enum")
alert_status_enum = sa.Enum("PENDING", "SENT", "FAILED", "SUPPRESSED", name="alert_status_enum")
transaction_side_enum = sa.Enum("BUY", "SELL", name="transaction_side_enum")


def upgrade() -> None:
    op.create_table(
        "strategy_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", strategy_profile_code_enum, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("rule_overrides", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "user_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_key", sa.String(length=80), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("default_quote_currency", sa.String(length=10), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
        sa.Column("morning_digest_hour", sa.Integer(), nullable=False),
        sa.Column("morning_digest_minute", sa.Integer(), nullable=False),
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_key"),
    )

    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("coingecko_id", sa.String(length=120), nullable=True),
        sa.Column("binance_symbol", sa.String(length=40), nullable=True),
        sa.Column("bybit_symbol", sa.String(length=40), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_index("ix_assets_symbol", "assets", ["symbol"], unique=False)
    op.create_index("ix_assets_coingecko_id", "assets", ["coingecko_id"], unique=False)

    op.create_table(
        "portfolios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_currency", sa.String(length=10), nullable=False),
        sa.Column("strategy_profile_id", sa.Uuid(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("morning_digest_enabled", sa.Boolean(), nullable=False),
        sa.Column("alerts_enabled", sa.Boolean(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
        sa.Column("risk_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["strategy_profile_id"], ["strategy_profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_portfolios_strategy_profile_id", "portfolios", ["strategy_profile_id"], unique=False)

    op.create_table(
        "positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 12), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("cost_basis", sa.Numeric(30, 12), nullable=False),
        sa.Column("target_weight_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_manual_update_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portfolio_id", "asset_id", name="uq_portfolio_asset"),
    )
    op.create_index("ix_positions_portfolio_id", "positions", ["portfolio_id"], unique=False)
    op.create_index("ix_positions_asset_id", "positions", ["asset_id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("side", transaction_side_enum, nullable=False),
        sa.Column("quantity", sa.Numeric(30, 12), nullable=False),
        sa.Column("unit_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("fee_amount", sa.Numeric(30, 12), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_portfolio_id", "transactions", ["portfolio_id"], unique=False)
    op.create_index("ix_transactions_asset_id", "transactions", ["asset_id"], unique=False)
    op.create_index("ix_transactions_position_id", "transactions", ["position_id"], unique=False)

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("price_usd", sa.Numeric(30, 12), nullable=False),
        sa.Column("market_cap_usd", sa.Numeric(30, 2), nullable=True),
        sa.Column("volume_24h_usd", sa.Numeric(30, 2), nullable=True),
        sa.Column("change_1h_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("change_24h_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_snapshots_asset_id", "price_snapshots", ["asset_id"], unique=False)
    op.create_index(
        "ix_price_snapshot_asset_captured",
        "price_snapshots",
        ["asset_id", "captured_at"],
        unique=False,
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("signal_type", signal_type_enum, nullable=False),
        sa.Column("severity", signal_severity_enum, nullable=False),
        sa.Column("confidence_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("action_idea", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("risk_note", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signals_portfolio_id", "signals", ["portfolio_id"], unique=False)
    op.create_index("ix_signals_asset_id", "signals", ["asset_id"], unique=False)
    op.create_index("ix_signals_position_id", "signals", ["position_id"], unique=False)
    op.create_index("ix_signals_event_key", "signals", ["event_key"], unique=False)
    op.create_index("ix_signal_portfolio_created", "signals", ["portfolio_id", "created_at"], unique=False)

    op.create_table(
        "alert_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("signal_id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("channel", alert_channel_enum, nullable=False),
        sa.Column("destination", sa.String(length=120), nullable=True),
        sa.Column("status", alert_status_enum, nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alert_events_signal_id", "alert_events", ["signal_id"], unique=False)
    op.create_index("ix_alert_events_portfolio_id", "alert_events", ["portfolio_id"], unique=False)
    op.create_index("ix_alert_events_dedupe_key", "alert_events", ["dedupe_key"], unique=False)

    op.create_table(
        "daily_digests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("sent_to_telegram", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_daily_digests_portfolio_id", "daily_digests", ["portfolio_id"], unique=False)
    op.create_index(
        "ix_digest_portfolio_created",
        "daily_digests",
        ["portfolio_id", "created_at"],
        unique=False,
    )

    strategy_profiles = sa.table(
        "strategy_profiles",
        sa.column("id", sa.Uuid()),
        sa.column("code", strategy_profile_code_enum),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("rule_overrides", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        strategy_profiles,
        [
            {
                "id": uuid.uuid4(),
                "code": "MAIN",
                "name": "Main Portfolio",
                "description": "More active spot portfolio with staged take-profit guidance.",
                "rule_overrides": {
                    "take_profit_style": "active",
                    "alert_frequency": "normal",
                },
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": uuid.uuid4(),
                "code": "LONG_TERM",
                "name": "Long-Term Kids Portfolio",
                "description": "Conservative long-term portfolio with lower alert frequency.",
                "rule_overrides": {
                    "take_profit_style": "conservative",
                    "alert_frequency": "low",
                },
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_digest_portfolio_created", table_name="daily_digests")
    op.drop_index("ix_daily_digests_portfolio_id", table_name="daily_digests")
    op.drop_table("daily_digests")

    op.drop_index("ix_alert_events_dedupe_key", table_name="alert_events")
    op.drop_index("ix_alert_events_portfolio_id", table_name="alert_events")
    op.drop_index("ix_alert_events_signal_id", table_name="alert_events")
    op.drop_table("alert_events")

    op.drop_index("ix_signal_portfolio_created", table_name="signals")
    op.drop_index("ix_signals_event_key", table_name="signals")
    op.drop_index("ix_signals_position_id", table_name="signals")
    op.drop_index("ix_signals_asset_id", table_name="signals")
    op.drop_index("ix_signals_portfolio_id", table_name="signals")
    op.drop_table("signals")

    op.drop_index("ix_price_snapshot_asset_captured", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_asset_id", table_name="price_snapshots")
    op.drop_table("price_snapshots")

    op.drop_index("ix_transactions_position_id", table_name="transactions")
    op.drop_index("ix_transactions_asset_id", table_name="transactions")
    op.drop_index("ix_transactions_portfolio_id", table_name="transactions")
    op.drop_table("transactions")

    op.drop_index("ix_positions_asset_id", table_name="positions")
    op.drop_index("ix_positions_portfolio_id", table_name="positions")
    op.drop_table("positions")

    op.drop_index("ix_portfolios_strategy_profile_id", table_name="portfolios")
    op.drop_table("portfolios")

    op.drop_index("ix_assets_coingecko_id", table_name="assets")
    op.drop_index("ix_assets_symbol", table_name="assets")
    op.drop_table("assets")

    op.drop_table("user_settings")
    op.drop_table("strategy_profiles")
