from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db.models import DailyDigest, Portfolio, Position, Signal, Transaction, UserSettings
from app.schemas.common import DailyDigestRead, SignalRead
from app.schemas.dashboard import (
    DashboardDigestRead,
    DashboardOverviewRead,
    DashboardPortfolioRead,
    DashboardPositionRead,
    DashboardTotalsRead,
    DashboardTransactionRead,
)


class DashboardService:
    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def build_overview(self) -> DashboardOverviewRead:
        portfolios = self._load_portfolios()
        settings_row = self.session.scalar(select(UserSettings).where(UserSettings.user_key == "default"))

        recent_signals = self._load_recent_signals(limit=12)
        recent_transactions = self._load_recent_transactions(limit=12)
        recent_digests = self._load_recent_digests(limit=6)

        total_cost_basis = sum(
            (Decimal(position.cost_basis) for portfolio in portfolios for position in portfolio.positions),
            start=Decimal("0"),
        )

        totals = DashboardTotalsRead(
            portfolio_count=len(portfolios),
            position_count=sum(len(portfolio.positions) for portfolio in portfolios),
            tracked_asset_count=len({position.asset_id for portfolio in portfolios for position in portfolio.positions}),
            active_signal_count=len(recent_signals),
            transaction_count=self.session.scalar(select(func.count(Transaction.id))) or 0,
            total_cost_basis=total_cost_basis,
        )

        return DashboardOverviewRead(
            app_name=self.settings.app_name,
            environment=self.settings.environment,
            generated_at=datetime.now(self.settings.tzinfo),
            timezone=settings_row.timezone if settings_row is not None else self.settings.timezone,
            default_quote_currency=(
                settings_row.default_quote_currency if settings_row is not None else self.settings.default_quote_currency
            ),
            provider_order=self.settings.provider_order,
            scheduler_enabled=self.settings.scheduler_enabled,
            telegram_enabled=bool(self.settings.telegram_bot_token),
            read_only=True,
            data_source=self._describe_data_source(),
            totals=totals,
            portfolios=[self._serialize_portfolio(portfolio) for portfolio in portfolios],
            recent_signals=[SignalRead.model_validate(signal) for signal in recent_signals],
            recent_transactions=[self._serialize_transaction(transaction) for transaction in recent_transactions],
            recent_digests=[self._serialize_digest(digest) for digest in recent_digests],
        )

    def _load_portfolios(self) -> list[Portfolio]:
        statement = (
            select(Portfolio)
            .options(
                selectinload(Portfolio.strategy_profile),
                selectinload(Portfolio.positions)
                .selectinload(Position.asset),
                selectinload(Portfolio.positions)
                .selectinload(Position.transactions),
                selectinload(Portfolio.signals),
                selectinload(Portfolio.digests),
                selectinload(Portfolio.transactions).selectinload(Transaction.asset),
            )
            .order_by(Portfolio.name.asc())
        )
        return list(self.session.scalars(statement).unique())

    def _load_recent_signals(self, *, limit: int) -> list[Signal]:
        statement = select(Signal).order_by(Signal.created_at.desc()).limit(limit)
        return list(self.session.scalars(statement))

    def _load_recent_transactions(self, *, limit: int) -> list[Transaction]:
        statement = (
            select(Transaction)
            .options(
                selectinload(Transaction.asset),
                selectinload(Transaction.portfolio),
            )
            .order_by(Transaction.executed_at.desc(), Transaction.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def _load_recent_digests(self, *, limit: int) -> list[DailyDigest]:
        statement = (
            select(DailyDigest)
            .options(selectinload(DailyDigest.portfolio))
            .order_by(DailyDigest.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def _serialize_portfolio(self, portfolio: Portfolio) -> DashboardPortfolioRead:
        total_cost_basis = sum(
            (Decimal(position.cost_basis) for position in portfolio.positions),
            start=Decimal("0"),
        )
        latest_digest = portfolio.digests[0] if portfolio.digests else None

        positions = [
            DashboardPositionRead(
                id=position.id,
                portfolio_id=position.portfolio_id,
                asset_id=position.asset_id,
                symbol=position.asset.symbol,
                asset_name=position.asset.name,
                quantity=Decimal(position.quantity),
                average_entry_price=Decimal(position.average_entry_price),
                cost_basis=Decimal(position.cost_basis),
                target_weight_pct=(
                    Decimal(position.target_weight_pct) if position.target_weight_pct is not None else None
                ),
                notes=position.notes,
                last_manual_update_at=position.last_manual_update_at,
                transaction_count=len(position.transactions),
            )
            for position in portfolio.positions
        ]

        recent_transactions = sorted(
            portfolio.transactions,
            key=lambda transaction: (transaction.executed_at, transaction.created_at),
            reverse=True,
        )[:5]

        return DashboardPortfolioRead(
            id=portfolio.id,
            name=portfolio.name,
            description=portfolio.description,
            strategy_profile_code=portfolio.strategy_profile.code,
            strategy_profile_name=portfolio.strategy_profile.name,
            strategy_profile_description=portfolio.strategy_profile.description,
            base_currency=portfolio.base_currency,
            is_active=portfolio.is_active,
            morning_digest_enabled=portfolio.morning_digest_enabled,
            alerts_enabled=portfolio.alerts_enabled,
            telegram_chat_configured=bool(portfolio.telegram_chat_id),
            risk_notes=portfolio.risk_notes,
            position_count=len(portfolio.positions),
            latest_signal_count=min(len(portfolio.signals), 10),
            total_cost_basis=total_cost_basis,
            positions=positions,
            recent_signals=[SignalRead.model_validate(signal) for signal in portfolio.signals[:4]],
            recent_transactions=[self._serialize_transaction(transaction) for transaction in recent_transactions],
            latest_digest=self._serialize_digest(latest_digest) if latest_digest is not None else None,
        )

    def _serialize_transaction(self, transaction: Transaction) -> DashboardTransactionRead:
        return DashboardTransactionRead(
            id=transaction.id,
            portfolio_id=transaction.portfolio_id,
            portfolio_name=transaction.portfolio.name,
            position_id=transaction.position_id,
            asset_id=transaction.asset_id,
            asset_symbol=transaction.asset.symbol,
            side=transaction.side,
            quantity=Decimal(transaction.quantity),
            unit_price=Decimal(transaction.unit_price),
            fee_amount=Decimal(transaction.fee_amount) if transaction.fee_amount is not None else None,
            executed_at=transaction.executed_at,
            notes=transaction.notes,
        )

    def _serialize_digest(self, digest: DailyDigest) -> DashboardDigestRead:
        preview = digest.content.splitlines()[0] if digest.content else "Digest ready"
        return DashboardDigestRead(
            id=digest.id,
            portfolio_id=digest.portfolio_id,
            portfolio_name=digest.portfolio.name,
            created_at=digest.created_at,
            sent_to_telegram=digest.sent_to_telegram,
            preview=preview,
            digest=DailyDigestRead.model_validate(digest),
        )

    def _describe_data_source(self) -> str:
        url = self.settings.database_url
        if url.startswith("sqlite"):
            return f"SQLite / {url.rsplit('/', maxsplit=1)[-1]}"

        parsed = urlparse(url)
        if parsed.scheme and parsed.hostname:
            db_name = parsed.path.rsplit("/", maxsplit=1)[-1] if parsed.path else ""
            suffix = f"/{db_name}" if db_name else ""
            return f"{parsed.scheme}://{parsed.hostname}{suffix}"
        return "Configured database"
