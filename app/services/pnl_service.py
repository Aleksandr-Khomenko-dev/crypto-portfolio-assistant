"""Realized P&L calculation and daily portfolio value history."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import Asset, Portfolio, Position, PriceSnapshot, Transaction, TransactionSide


# ── Realized P&L ────────────────────────────────────────────────────────────

@dataclass
class RealizedPnl:
    total_realized_usd: Decimal = Decimal("0")
    total_fees_usd: Decimal = Decimal("0")
    net_realized_usd: Decimal = Decimal("0")   # realized minus fees
    sell_count: int = 0
    per_asset: dict[str, Decimal] = field(default_factory=dict)


def compute_realized_pnl(session: Session, portfolio_id: UUID) -> RealizedPnl:
    """
    Realized P&L using FIFO across all sell transactions for a portfolio.
    Simple cost-basis approach: realized = qty_sold × (sell_price – avg_entry).
    """
    result = RealizedPnl()

    # Get all sell transactions ordered by time
    sells = list(session.execute(
        select(Transaction, Asset.symbol, Position.average_entry_price)
        .join(Asset, Transaction.asset_id == Asset.id)
        .outerjoin(Position, Transaction.position_id == Position.id)
        .where(
            Transaction.portfolio_id == portfolio_id,
            Transaction.side == TransactionSide.SELL,
        )
        .order_by(Transaction.executed_at.asc())
    ))

    if not sells:
        return result

    for tx, symbol, avg_entry in sells:
        qty = Decimal(str(tx.quantity))
        sell_price = Decimal(str(tx.unit_price))
        fee = Decimal(str(tx.fee_amount)) if tx.fee_amount else Decimal("0")
        entry = Decimal(str(avg_entry)) if avg_entry else Decimal("0")

        realized = (sell_price - entry) * qty
        result.total_realized_usd += realized
        result.total_fees_usd += fee
        result.sell_count += 1
        result.per_asset[symbol] = result.per_asset.get(symbol, Decimal("0")) + realized

    result.net_realized_usd = result.total_realized_usd - result.total_fees_usd
    return result


# ── Daily P&L history ────────────────────────────────────────────────────────

@dataclass
class DailyPoint:
    date: str            # YYYY-MM-DD
    portfolio_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


def portfolio_daily_history(
    session: Session,
    portfolio_id: UUID,
    days: int = 30,
) -> list[DailyPoint]:
    """
    Build a daily time-series of portfolio value from PriceSnapshot table.
    Returns one point per day (average of snapshots that day).
    """
    positions = list(session.scalars(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ))
    if not positions:
        return []

    asset_ids = [p.asset_id for p in positions]
    qty_map: dict[UUID, Decimal] = {p.asset_id: Decimal(str(p.quantity)) for p in positions}
    total_cost = sum(Decimal(str(p.cost_basis)) for p in positions)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)

    rows = list(session.execute(
        select(
            func.strftime("%Y-%m-%d", PriceSnapshot.captured_at).label("day"),
            PriceSnapshot.asset_id,
            func.avg(PriceSnapshot.price_usd).label("avg_price"),
            func.count(PriceSnapshot.asset_id).label("snap_count"),
        )
        .where(
            PriceSnapshot.asset_id.in_(asset_ids),
            PriceSnapshot.captured_at >= since,
        )
        .group_by(text("day"), PriceSnapshot.asset_id)
        .order_by(text("day"))
    ))

    # Group by day
    day_assets: dict[str, dict[UUID, float]] = defaultdict(dict)
    for day, asset_id, avg_price, _ in rows:
        if day and avg_price:
            day_assets[day][asset_id] = float(avg_price)

    result: list[DailyPoint] = []
    min_coverage = max(1, int(len(asset_ids) * 0.6))
    for day in sorted(day_assets.keys()):
        covered = day_assets[day]
        if len(covered) < min_coverage:
            continue
        total_val = sum(
            float(qty_map.get(aid, Decimal("0"))) * price
            for aid, price in covered.items()
        )
        if total_val <= 0:
            continue
        cost = float(total_cost)
        upnl = total_val - cost
        upnl_pct = (upnl / cost * 100) if cost else 0.0
        result.append(DailyPoint(
            date=day,
            portfolio_value=round(total_val, 2),
            unrealized_pnl=round(upnl, 2),
            unrealized_pnl_pct=round(upnl_pct, 2),
        ))

    return result
