"""Analytics endpoints: realized P&L, daily history, CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, Portfolio, Position, Transaction
from app.db.session import get_db
from app.services.pnl_service import DailyPoint, RealizedPnl, compute_realized_pnl, portfolio_daily_history

router = APIRouter(tags=["analytics"])


class RealizedPnlResponse(BaseModel):
    portfolio_id: UUID
    total_realized_usd: float
    total_fees_usd: float
    net_realized_usd: float
    sell_count: int
    per_asset: dict[str, float]


class DailyHistoryResponse(BaseModel):
    portfolio_id: UUID
    days: int
    points: list[DailyPoint]


# ── Realized P&L ─────────────────────────────────────────────────────────────

@router.get("/portfolios/{portfolio_id}/pnl/realized", response_model=RealizedPnlResponse)
def realized_pnl(
    portfolio_id: UUID,
    db: Session = Depends(get_db),
) -> RealizedPnlResponse:
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    pnl = compute_realized_pnl(db, portfolio_id)
    return RealizedPnlResponse(
        portfolio_id=portfolio_id,
        total_realized_usd=float(pnl.total_realized_usd),
        total_fees_usd=float(pnl.total_fees_usd),
        net_realized_usd=float(pnl.net_realized_usd),
        sell_count=pnl.sell_count,
        per_asset={k: float(v) for k, v in pnl.per_asset.items()},
    )


# ── Daily history ─────────────────────────────────────────────────────────────

@router.get("/portfolios/{portfolio_id}/pnl/history", response_model=DailyHistoryResponse)
def pnl_history(
    portfolio_id: UUID,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> DailyHistoryResponse:
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    points = portfolio_daily_history(db, portfolio_id, days=days)
    return DailyHistoryResponse(portfolio_id=portfolio_id, days=days, points=points)


# ── CSV export ────────────────────────────────────────────────────────────────

@router.get("/portfolios/{portfolio_id}/export/csv")
def export_positions_csv(
    portfolio_id: UUID,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Export current positions with live (last snapshot) prices as CSV."""
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    positions = list(db.scalars(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ))
    if not positions:
        raise HTTPException(status_code=404, detail="No positions in portfolio")

    # Fetch latest snapshots
    from app.api.routers.dashboard import _get_latest_snapshots
    asset_ids = [p.asset_id for p in positions]
    snapshots = _get_latest_snapshots(db, asset_ids)

    realized = compute_realized_pnl(db, portfolio_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Symbol", "Name", "Quantity", "Avg Entry Price (USD)",
        "Current Price (USD)", "Cost Basis (USD)", "Current Value (USD)",
        "Unrealized P&L (USD)", "Unrealized P&L (%)",
        "Realized P&L (USD)", "TP Target (USD)", "SL Target (USD)",
        "Price Updated At",
    ])

    from decimal import Decimal
    total_cost = Decimal("0")
    total_value = Decimal("0")

    for pos in sorted(positions, key=lambda p: p.asset.symbol):
        qty = Decimal(str(pos.quantity))
        avg_entry = Decimal(str(pos.average_entry_price))
        cost = Decimal(str(pos.cost_basis))
        snap = snapshots.get(pos.asset_id)
        if snap and snap.price_usd:
            cur_price = Decimal(str(snap.price_usd))
            cur_value = qty * cur_price
            upnl = cur_value - cost
            upnl_pct = (upnl / cost * 100) if cost else Decimal("0")
            updated_at = snap.captured_at.strftime("%Y-%m-%d %H:%M UTC")
        else:
            cur_price = cur_value = upnl = upnl_pct = Decimal("0")
            updated_at = "N/A"

        sym = pos.asset.symbol
        realized_asset = realized.per_asset.get(sym, Decimal("0"))
        tp = pos.price_alert_take_profit_usd
        sl = pos.price_alert_stop_loss_usd

        writer.writerow([
            sym,
            pos.asset.name or "",
            f"{qty:.8f}",
            f"{avg_entry:.8f}",
            f"{cur_price:.8f}",
            f"{cost:.2f}",
            f"{cur_value:.2f}",
            f"{upnl:.2f}",
            f"{upnl_pct:.2f}",
            f"{float(realized_asset):.2f}",
            f"{float(tp):.8f}" if tp else "",
            f"{float(sl):.8f}" if sl else "",
            updated_at,
        ])
        total_cost += cost
        total_value += cur_value

    total_upnl = total_value - total_cost
    writer.writerow([])
    writer.writerow([
        "TOTAL", "", "", "",
        "",
        f"{total_cost:.2f}",
        f"{total_value:.2f}",
        f"{total_upnl:.2f}",
        f"{float(total_upnl / total_cost * 100) if total_cost else 0:.2f}",
        f"{float(realized.net_realized_usd):.2f}",
        "", "", "",
    ])

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"portfolio_{portfolio.name}_{ts}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/portfolios/{portfolio_id}/export/transactions/csv")
def export_transactions_csv(
    portfolio_id: UUID,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Export all transactions as CSV for tax/accounting purposes."""
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    rows = list(db.execute(
        select(Transaction, Asset.symbol, Asset.name)
        .join(Asset, Transaction.asset_id == Asset.id)
        .where(Transaction.portfolio_id == portfolio_id)
        .order_by(Transaction.executed_at.asc())
    ))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Date", "Symbol", "Name", "Side", "Quantity",
        "Unit Price (USD)", "Total (USD)", "Fee (USD)", "Notes",
    ])

    for tx, symbol, name in rows:
        from decimal import Decimal
        qty = Decimal(str(tx.quantity))
        price = Decimal(str(tx.unit_price))
        fee = Decimal(str(tx.fee_amount)) if tx.fee_amount else Decimal("0")
        total = qty * price
        writer.writerow([
            tx.executed_at.strftime("%Y-%m-%d %H:%M"),
            symbol,
            name or "",
            tx.side.value.upper(),
            f"{qty:.8f}",
            f"{price:.8f}",
            f"{total:.2f}",
            f"{fee:.2f}",
            tx.notes or "",
        ])

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"transactions_{portfolio.name}_{ts}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
