from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_market_provider
from app.config import Settings, get_settings
from app.db.models import Asset, DailyDigest, Portfolio, Position, PriceSnapshot, Signal, Transaction
from app.db.session import get_db
from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote
from app.services.market_service import MarketService
from app.schemas.dashboard import (
    DashboardDigestContent,
    DashboardDigestPreview,
    DashboardOverview,
    DashboardPortfolio,
    DashboardPosition,
    DashboardRecentDigest,
    DashboardSignal,
    DashboardTotals,
    DashboardTransaction,
)

router = APIRouter(tags=["dashboard"])
logger = logging.getLogger(__name__)


@router.get("/dashboard/overview", response_model=DashboardOverview)
async def dashboard_overview(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    market_provider: AbstractMarketDataProvider = Depends(get_market_provider),
) -> DashboardOverview:
    raw_portfolios = _load_portfolios_orm(db)
    live_quotes = await _fetch_live_quotes(db, market_provider, raw_portfolios)
    portfolios = _build_dashboard_portfolios(db, raw_portfolios, live_quotes)
    all_signals = _load_recent_signals(db, limit=30)
    all_digests = _load_recent_digests(db, limit=10)
    totals = _compute_totals(db, portfolios)

    return DashboardOverview(
        environment=settings.environment,
        data_source=settings.database_url.split("://")[0],
        provider_order=list(settings.provider_order),
        scheduler_enabled=settings.scheduler_enabled,
        telegram_enabled=bool(settings.telegram_bot_token),
        timezone=settings.timezone,
        default_quote_currency=settings.default_quote_currency,
        totals=totals,
        portfolios=portfolios,
        recent_signals=all_signals,
        recent_digests=all_digests,
    )


def _get_latest_snapshots(db: Session, asset_ids: list[UUID]) -> dict[UUID, PriceSnapshot]:
    """Return the most recent PriceSnapshot per asset_id for a list of asset IDs."""
    if not asset_ids:
        return {}
    subq = (
        select(
            PriceSnapshot.asset_id,
            func.max(PriceSnapshot.captured_at).label("latest_at"),
        )
        .where(PriceSnapshot.asset_id.in_(asset_ids))
        .group_by(PriceSnapshot.asset_id)
        .subquery()
    )
    stmt = select(PriceSnapshot).join(
        subq,
        (PriceSnapshot.asset_id == subq.c.asset_id)
        & (PriceSnapshot.captured_at == subq.c.latest_at),
    )
    return {snap.asset_id: snap for snap in db.scalars(stmt)}


def _load_portfolios_orm(db: Session) -> list[Portfolio]:
    stmt = (
        select(Portfolio)
        .options(
            selectinload(Portfolio.strategy_profile),
            selectinload(Portfolio.positions).selectinload(Position.asset),
        )
        .order_by(Portfolio.name.asc())
    )
    return list(db.scalars(stmt).unique())


def _collect_unique_asset_queries(portfolios: list[Portfolio]) -> list[AssetMarketQuery]:
    by_id: dict[UUID, AssetMarketQuery] = {}
    for portfolio in portfolios:
        for pos in portfolio.positions:
            if pos.asset_id in by_id:
                continue
            asset = pos.asset
            by_id[pos.asset_id] = AssetMarketQuery(
                asset_id=asset.id,
                symbol=asset.symbol,
                name=asset.name,
                coingecko_id=asset.coingecko_id,
                binance_symbol=asset.binance_symbol,
                bybit_symbol=asset.bybit_symbol,
            )
    return list(by_id.values())


async def _fetch_live_quotes(
    db: Session,
    market_provider: AbstractMarketDataProvider,
    portfolios: list[Portfolio],
) -> dict[UUID, MarketQuote]:
    queries = _collect_unique_asset_queries(portfolios)
    if not queries:
        return {}
    try:
        return await MarketService(db, market_provider).fetch_quotes_for_queries(queries)
    except Exception:
        logger.exception("Dashboard live quote fetch failed; falling back to latest DB snapshots")
        return {}


def _build_dashboard_portfolios(
    db: Session,
    raw_portfolios: list[Portfolio],
    live_quotes: dict[UUID, MarketQuote],
) -> list[DashboardPortfolio]:
    result: list[DashboardPortfolio] = []
    for p in raw_portfolios:
        positions = _load_positions(db, p.id, live_quotes)
        signals = _load_portfolio_signals(db, p.id, limit=10)
        transactions = _load_portfolio_transactions(db, p.id, limit=20)
        latest_digest = _load_latest_digest(db, p.id)
        total_cost = sum((pos.cost_basis for pos in positions), Decimal("0"))
        total_value = sum((pos.current_value for pos in positions), Decimal("0"))
        total_pnl = total_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else Decimal("0")
        # Weighted 24h change
        live_positions = [pos for pos in positions if pos.change_24h_pct is not None and pos.current_value > 0]
        if live_positions:
            total_live_value = sum(pos.current_value for pos in live_positions)
            w24h = sum(
                pos.change_24h_pct * pos.current_value / total_live_value  # type: ignore[operator]
                for pos in live_positions
            )
        else:
            w24h = None

        result.append(DashboardPortfolio(
            id=p.id,
            name=p.name,
            description=p.description,
            base_currency=p.base_currency,
            strategy_profile_code=p.strategy_profile.code.value,
            strategy_profile_name=p.strategy_profile.name,
            strategy_profile_description=p.strategy_profile.description,
            is_active=p.is_active,
            alerts_enabled=p.alerts_enabled,
            morning_digest_enabled=p.morning_digest_enabled,
            telegram_chat_configured=bool(p.telegram_chat_id),
            risk_notes=p.risk_notes,
            total_cost_basis=total_cost,
            total_current_value=total_value,
            total_pnl_usd=total_pnl,
            total_pnl_pct=total_pnl_pct,
            total_change_24h_pct=w24h,
            position_count=len(p.positions),
            latest_signal_count=len(signals),
            positions=positions,
            recent_signals=signals,
            recent_transactions=transactions,
            latest_digest=latest_digest,
        ))

    return result


def _load_positions(
    db: Session,
    portfolio_id: UUID,
    live_quotes: dict[UUID, MarketQuote],
) -> list[DashboardPosition]:
    stmt = (
        select(Position)
        .where(Position.portfolio_id == portfolio_id)
        .order_by(Position.created_at.asc())
    )
    positions = list(db.scalars(stmt))

    asset_ids = [pos.asset_id for pos in positions]
    snapshots = _get_latest_snapshots(db, asset_ids)

    result: list[DashboardPosition] = []
    for pos in positions:
        tx_count = db.scalar(
            select(func.count()).where(Transaction.position_id == pos.id)
        ) or 0

        qty = Decimal(str(pos.quantity))
        avg_entry = Decimal(str(pos.average_entry_price))
        cost_basis = Decimal(str(pos.cost_basis))

        quote = live_quotes.get(pos.asset_id)
        snap = snapshots.get(pos.asset_id)
        if quote is not None:
            try:
                current_price = Decimal(str(quote.price_usd))
            except InvalidOperation:
                current_price = Decimal("0")
            current_value = qty * current_price
            pnl_usd = current_value - cost_basis
            pnl_pct = (pnl_usd / cost_basis * 100) if cost_basis else Decimal("0")
            change_1h = Decimal(str(quote.change_1h_pct)) if quote.change_1h_pct is not None else None
            change_24h = Decimal(str(quote.change_24h_pct)) if quote.change_24h_pct is not None else None
            price_updated_at = quote.source_timestamp or datetime.now(timezone.utc)
        elif snap and snap.price_usd:
            try:
                current_price = Decimal(str(snap.price_usd))
            except InvalidOperation:
                current_price = Decimal("0")
            current_value = qty * current_price
            pnl_usd = current_value - cost_basis
            pnl_pct = (pnl_usd / cost_basis * 100) if cost_basis else Decimal("0")
            change_1h = Decimal(str(snap.change_1h_pct)) if snap.change_1h_pct is not None else None
            change_24h = Decimal(str(snap.change_24h_pct)) if snap.change_24h_pct is not None else None
            price_updated_at = snap.captured_at
        else:
            current_price = Decimal("0")
            current_value = Decimal("0")
            pnl_usd = Decimal("0")
            pnl_pct = Decimal("0")
            change_1h = None
            change_24h = None
            price_updated_at = None

        result.append(DashboardPosition(
            id=pos.id,
            symbol=pos.asset.symbol,
            asset_name=pos.asset.name,
            quantity=qty,
            average_entry_price=avg_entry,
            cost_basis=cost_basis,
            current_price=current_price,
            current_value=current_value,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            change_1h_pct=change_1h,
            change_24h_pct=change_24h,
            price_updated_at=price_updated_at,
            price_alert_take_profit_usd=(
                Decimal(str(pos.price_alert_take_profit_usd))
                if pos.price_alert_take_profit_usd is not None
                else None
            ),
            price_alert_stop_loss_usd=(
                Decimal(str(pos.price_alert_stop_loss_usd))
                if pos.price_alert_stop_loss_usd is not None
                else None
            ),
            target_weight_pct=Decimal(str(pos.target_weight_pct)) if pos.target_weight_pct is not None else None,
            notes=pos.notes,
            transaction_count=tx_count,
        ))
    return result


def _load_portfolio_signals(db: Session, portfolio_id: UUID, limit: int) -> list[DashboardSignal]:
    stmt = (
        select(Signal)
        .where(Signal.portfolio_id == portfolio_id)
        .order_by(Signal.created_at.desc())
        .limit(limit)
    )
    return [
        DashboardSignal(
            id=s.id,
            portfolio_id=s.portfolio_id,
            signal_type=s.signal_type,
            severity=s.severity,
            title=s.title,
            message=s.message,
            action_idea=s.action_idea,
            created_at=s.created_at,
        )
        for s in db.scalars(stmt)
    ]


def _load_portfolio_transactions(db: Session, portfolio_id: UUID, limit: int) -> list[DashboardTransaction]:
    stmt = (
        select(Transaction, Asset.symbol)
        .join(Asset, Transaction.asset_id == Asset.id)
        .where(Transaction.portfolio_id == portfolio_id)
        .order_by(Transaction.executed_at.desc())
        .limit(limit)
    )
    result: list[DashboardTransaction] = []
    for tx, symbol in db.execute(stmt):
        result.append(DashboardTransaction(
            id=tx.id,
            portfolio_id=tx.portfolio_id,
            position_id=tx.position_id,
            asset_symbol=symbol,
            side=tx.side,
            quantity=Decimal(tx.quantity),
            unit_price=Decimal(tx.unit_price),
            fee_amount=Decimal(tx.fee_amount) if tx.fee_amount is not None else None,
            notes=tx.notes,
            executed_at=tx.executed_at,
        ))
    return result


def _load_latest_digest(db: Session, portfolio_id: UUID) -> DashboardDigestPreview | None:
    stmt = (
        select(DailyDigest)
        .where(DailyDigest.portfolio_id == portfolio_id)
        .order_by(DailyDigest.created_at.desc())
        .limit(1)
    )
    digest = db.scalar(stmt)
    if digest is None:
        return None
    preview = digest.content.split("\n")[0][:120]
    return DashboardDigestPreview(
        id=digest.id,
        created_at=digest.created_at,
        sent_to_telegram=digest.sent_to_telegram,
        preview=preview,
        digest=DashboardDigestContent(content=digest.content),
    )


def _load_recent_signals(db: Session, limit: int) -> list[DashboardSignal]:
    stmt = (
        select(Signal)
        .order_by(Signal.created_at.desc())
        .limit(limit)
    )
    return [
        DashboardSignal(
            id=s.id,
            portfolio_id=s.portfolio_id,
            signal_type=s.signal_type,
            severity=s.severity,
            title=s.title,
            message=s.message,
            action_idea=s.action_idea,
            created_at=s.created_at,
        )
        for s in db.scalars(stmt)
    ]


def _load_recent_digests(db: Session, limit: int) -> list[DashboardRecentDigest]:
    stmt = (
        select(DailyDigest, Portfolio.name.label("portfolio_name"))
        .join(Portfolio, DailyDigest.portfolio_id == Portfolio.id)
        .order_by(DailyDigest.created_at.desc())
        .limit(limit)
    )
    result: list[DashboardRecentDigest] = []
    for digest, portfolio_name in db.execute(stmt):
        result.append(DashboardRecentDigest(
            portfolio_id=digest.portfolio_id,
            portfolio_name=portfolio_name,
            created_at=digest.created_at,
            sent_to_telegram=digest.sent_to_telegram,
            preview=digest.content.split("\n")[0][:120],
        ))
    return result


def _compute_totals(db: Session, portfolios: list[DashboardPortfolio]) -> DashboardTotals:
    total_cost = sum((p.total_cost_basis for p in portfolios), Decimal("0"))
    total_value = sum((p.total_current_value for p in portfolios), Decimal("0"))
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else Decimal("0")
    position_count = sum(p.position_count for p in portfolios)
    signal_count = db.scalar(select(func.count()).select_from(Signal)) or 0
    tx_count = db.scalar(select(func.count()).select_from(Transaction)) or 0
    asset_count = db.scalar(select(func.count()).select_from(Asset)) or 0

    return DashboardTotals(
        portfolio_count=len(portfolios),
        position_count=position_count,
        tracked_asset_count=asset_count,
        total_cost_basis=total_cost,
        total_current_value=total_value,
        total_pnl_usd=total_pnl,
        total_pnl_pct=total_pnl_pct,
        active_signal_count=signal_count,
        transaction_count=tx_count,
    )
