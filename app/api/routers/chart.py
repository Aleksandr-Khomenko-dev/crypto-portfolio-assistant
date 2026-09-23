from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import Portfolio, Position, PriceSnapshot
from app.db.session import get_db

router = APIRouter(tags=["chart"])


class ChartPoint(BaseModel):
    ts: str        # ISO-8601 timestamp
    value: float   # total portfolio value at this point


class ChartLeader(BaseModel):
    symbol: str
    change_usd: float
    change_pct: float


class ChartResponse(BaseModel):
    period: str
    points: list[ChartPoint]
    current_value: float
    cost_basis: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    realized_pnl: float          # placeholder – 0 for now
    value_min: float
    value_max: float
    value_min_ts: str
    value_max_ts: str
    top_gainer: ChartLeader | None
    top_loser: ChartLeader | None
    change_24h_usd: float
    change_24h_pct: float


_BUCKET_SQL: dict[str, str] = {
    # SQLite strftime patterns
    "24h":  "%Y-%m-%d %H:00:00",
    "7d":   "%Y-%m-%d %H:00:00",
    "1m":   "%Y-%m-%d 00:00:00",
    "3m":   "%Y-%m-%d 00:00:00",
    "ytd":  "%Y-%m-%d 00:00:00",
    "1yr":  "%Y-%W 00:00:00",
    "all":  "%Y-%W 00:00:00",
}

_PERIOD_DELTA: dict[str, timedelta | None] = {
    "24h":  timedelta(hours=24),
    "7d":   timedelta(days=7),
    "1m":   timedelta(days=30),
    "3m":   timedelta(days=90),
    "ytd":  None,    # computed dynamically below
    "1yr":  timedelta(days=365),
    "all":  None,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/portfolios/{portfolio_id}/chart", response_model=ChartResponse)
def portfolio_chart(
    portfolio_id: UUID,
    period: str = Query(default="7d", pattern="^(24h|7d|1m|3m|ytd|1yr|all)$"),
    db: Session = Depends(get_db),
) -> ChartResponse:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    positions = list(db.scalars(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ))
    if not positions:
        return _empty_response(period)

    asset_ids = [p.asset_id for p in positions]
    qty_map: dict[UUID, Decimal] = {p.asset_id: Decimal(str(p.quantity)) for p in positions}
    cost_map: dict[UUID, Decimal] = {p.asset_id: Decimal(str(p.cost_basis)) for p in positions}
    total_cost = sum(cost_map.values())

    now = _utcnow()
    if period == "ytd":
        # Year-to-date: Jan 1 of current year
        since = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    else:
        delta = _PERIOD_DELTA[period]
        since = (now - delta).replace(tzinfo=None) if delta else None

    # Build time-bucketed portfolio values using SQLite strftime
    bucket_fmt = _BUCKET_SQL[period]

    # For 7d, use 6-hour buckets to reduce noise
    if period == "7d":
        bucket_fmt = "%Y-%m-%d %H:00:00"

    # Get the latest price per asset per bucket + count of distinct assets
    bucket_col = func.strftime(bucket_fmt, PriceSnapshot.captured_at).label("bucket")
    stmt = (
        select(
            bucket_col,
            PriceSnapshot.asset_id,
            func.avg(PriceSnapshot.price_usd).label("avg_price"),
        )
        .where(PriceSnapshot.asset_id.in_(asset_ids))
        .group_by(text("bucket"), PriceSnapshot.asset_id)
        .order_by(text("bucket"))
    )
    if since:
        stmt = stmt.where(PriceSnapshot.captured_at >= since)

    rows = list(db.execute(stmt))

    if not rows:
        return _empty_response(period)

    # Group by bucket → track asset count and value per bucket
    bucket_totals: dict[str, float] = {}
    bucket_asset_count: dict[str, int] = {}
    for bucket, asset_id, avg_price in rows:
        if bucket is None or avg_price is None:
            continue
        value = float(Decimal(str(avg_price)) * qty_map.get(asset_id, Decimal("0")))
        bucket_totals[bucket] = bucket_totals.get(bucket, 0.0) + value
        bucket_asset_count[bucket] = bucket_asset_count.get(bucket, 0) + 1

    # Filter out buckets with less than 60% of assets covered (incomplete snapshots)
    min_assets = max(1, int(len(asset_ids) * 0.6))
    bucket_totals = {k: v for k, v in bucket_totals.items()
                     if bucket_asset_count.get(k, 0) >= min_assets}

    # For 7d reduce to every 6th hour entry to avoid clutter
    if period == "7d":
        keys = sorted(bucket_totals)
        step = max(1, len(keys) // 40)
        keep = set(keys[i] for i in range(0, len(keys), step)) | {keys[-1]}
        bucket_totals = {k: v for k, v in bucket_totals.items() if k in keep}

    points: list[ChartPoint] = [
        ChartPoint(ts=bucket + "Z", value=round(v, 2))
        for bucket, v in sorted(bucket_totals.items())
        if v > 0
    ]

    if not points:
        return _empty_response(period)

    values = [p.value for p in points]
    vmin = min(values)
    vmax = max(values)
    min_ts = points[values.index(vmin)].ts
    max_ts = points[values.index(vmax)].ts
    current_val = points[-1].value

    # 24h change
    change_24h_usd = 0.0
    change_24h_pct = 0.0
    if len(points) >= 2:
        prev = points[0].value
        change_24h_usd = current_val - prev
        change_24h_pct = (change_24h_usd / prev * 100) if prev else 0.0

    pnl = current_val - float(total_cost)
    pnl_pct = (pnl / float(total_cost) * 100) if total_cost else 0.0

    # Per-asset 24h change leaders
    snap_stmt = (
        select(
            PriceSnapshot.asset_id,
            func.avg(PriceSnapshot.change_24h_pct).label("ch24"),
            func.avg(PriceSnapshot.price_usd).label("price"),
        )
        .where(
            PriceSnapshot.asset_id.in_(asset_ids),
            PriceSnapshot.captured_at >= (now - timedelta(hours=2)).replace(tzinfo=None),
        )
        .group_by(PriceSnapshot.asset_id)
    )
    snap_rows = list(db.execute(snap_stmt))

    leaders: list[tuple[str, float, float]] = []
    for asset_id, ch24, price in snap_rows:
        if ch24 is None or price is None:
            continue
        sym = next((p.asset.symbol for p in positions if p.asset_id == asset_id), "?")
        qty = float(qty_map.get(asset_id, Decimal("0")))
        val_usd = qty * float(price) * float(ch24) / 100
        leaders.append((sym, round(val_usd, 2), round(float(ch24), 2)))

    top_gainer = None
    top_loser = None
    if leaders:
        gainers = [l for l in leaders if l[2] > 0]
        losers  = [l for l in leaders if l[2] < 0]
        if gainers:
            g = max(gainers, key=lambda x: x[2])
            top_gainer = ChartLeader(symbol=g[0], change_usd=g[1], change_pct=g[2])
        if losers:
            lo = min(losers, key=lambda x: x[2])
            top_loser = ChartLeader(symbol=lo[0], change_usd=lo[1], change_pct=lo[2])

    return ChartResponse(
        period=period,
        points=points,
        current_value=round(current_val, 2),
        cost_basis=round(float(total_cost), 2),
        unrealized_pnl=round(pnl, 2),
        unrealized_pnl_pct=round(pnl_pct, 2),
        realized_pnl=0.0,
        value_min=round(vmin, 2),
        value_max=round(vmax, 2),
        value_min_ts=min_ts,
        value_max_ts=max_ts,
        top_gainer=top_gainer,
        top_loser=top_loser,
        change_24h_usd=round(change_24h_usd, 2),
        change_24h_pct=round(change_24h_pct, 2),
    )


def _empty_response(period: str) -> ChartResponse:
    return ChartResponse(
        period=period, points=[], current_value=0, cost_basis=0,
        unrealized_pnl=0, unrealized_pnl_pct=0, realized_pnl=0,
        value_min=0, value_max=0, value_min_ts="", value_max_ts="",
        top_gainer=None, top_loser=None,
        change_24h_usd=0, change_24h_pct=0,
    )
