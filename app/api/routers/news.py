"""Read-only news context endpoints (context only; never part of trading scores)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.scanner_models import NewsClassification, NewsEntity, NewsItem
from app.db.session import get_db

router = APIRouter(prefix="/news", tags=["news"])
Database = Annotated[Session, Depends(get_db)]


@router.get("/status")
def status() -> dict:
    """Latency percentiles, queue depth and per-provider SLO of this process."""
    from app.news import service

    if service.RUNNING is None:
        return {"running": False}
    return {"running": True, **service.RUNNING.snapshot()}


@router.get("/{symbol}")
def symbol_news(
    db: Database,
    symbol: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{2,20}$")],
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    """Normalized recent news for a symbol, shaped for a future TradingView
    `news_provider` (title, published, shortDescription, link, source)."""
    pair = (
        symbol.upper() if symbol.upper().endswith("USDT") else symbol.upper() + "USDT"
    )
    rows = db.execute(
        select(NewsItem, NewsClassification, NewsEntity)
        .join(NewsEntity, NewsEntity.news_item_id == NewsItem.id)
        .join(NewsClassification, NewsClassification.news_item_id == NewsItem.id)
        .where(
            NewsEntity.symbol == pair,
            NewsClassification.noise.is_(False),
            NewsItem.received_at >= datetime.now(UTC) - timedelta(hours=hours),
        )
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "title": item.title,
            "shortDescription": item.summary[:280],
            "link": item.url,
            "source": item.source,
            "published": item.published_at.replace(tzinfo=UTC).isoformat()
            if item.published_at.tzinfo is None
            else item.published_at.isoformat(),
            "received": item.received_at.isoformat(),
            "symbol": pair,
            "match_type": entity.match_type,
            "event_type": c.event_type,
            "direction": c.direction,
            "importance": c.importance_score,
            "level": c.importance_level,
            "verification": c.verification,
        }
        for item, c, entity in rows
    ]
