"""News persistence (synchronous; called via asyncio.to_thread). Append-only.

Every write is a short transaction started after all network I/O for that item.
Uniqueness (provider item, cluster/setup link, alert dedupe key) is enforced with
INSERT ... ON CONFLICT DO NOTHING so restarts and concurrent workers stay idempotent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.scanner_models import (
    MarketSetup,
    NewsAlert,
    NewsClassification,
    NewsClusterStatus,
    NewsEntity,
    NewsEventCluster,
    NewsItem,
    NewsSetupLink,
)
from app.news.cluster import ClusterState
from app.news.domain import EntityMatch, ImpactLevel, ProcessedNews


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _insert(session: Session):
    return sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert


@dataclass(frozen=True)
class ActiveSetupRef:
    setup_id: uuid.UUID
    exchange: str
    symbol: str
    direction: str
    score: int
    state: str
    readiness: str
    created_at: datetime
    telegram_message_id: int | None


def active_setups(session: Session, now: datetime) -> list[ActiveSetupRef]:
    rows = session.scalars(
        select(MarketSetup).where(
            MarketSetup.lifecycle == "ACTIVE", MarketSetup.expires_at > now
        )
    )
    return [
        ActiveSetupRef(
            setup_id=row.id,
            exchange=row.exchange,
            symbol=row.symbol,
            direction=row.direction,
            score=row.score,
            state=row.state,
            readiness=row.readiness,
            created_at=_utc(row.created_at),
            telegram_message_id=(row.notified_data or {}).get("telegram_message_id"),
        )
        for row in rows
    ]


def store_processed(
    session: Session, news: ProcessedNews, cluster: ClusterState, cluster_is_new: bool
) -> bool:
    """Persist item, entities, classification and cluster status. False = duplicate."""
    item = news.item
    with session.begin():
        if cluster_is_new:
            session.add(
                NewsEventCluster(
                    id=cluster.id,
                    event_type=cluster.event_type.value,
                    symbols=sorted(cluster.symbols),
                    canonical_item_id=cluster.canonical_item_id,
                    canonical_url=cluster.canonical_url,
                    first_received_at=cluster.first_received_at,
                )
            )
            session.flush()
        inserted = session.scalar(
            _insert(session)(NewsItem)
            .values(
                id=item.id,
                provider=item.provider,
                provider_item_id=item.provider_item_id,
                source=item.source,
                source_type=int(item.source_type),
                title=item.title[:500],
                summary=item.summary[:2000],
                url=item.url,
                published_at=item.published_at,
                received_at=item.received_at,
                symbols=item.symbols,
                event_type_hint=item.event_type_hint,
                raw_payload_hash=item.raw_payload_hash,
                cluster_id=cluster.id,
                stages={k: v.isoformat() for k, v in news.stages.items()},
            )
            .on_conflict_do_nothing(index_elements=["provider", "provider_item_id"])
            .returning(NewsItem.id)
        )
        if inserted is None:
            return False
        for match in news.matches:
            session.add(
                NewsEntity(
                    news_item_id=item.id,
                    symbol=match.symbol,
                    match_type=match.match_type.value,
                    confidence=match.confidence,
                    matched_text=match.matched_text[:120],
                )
            )
        c = news.classification
        session.add(
            NewsClassification(
                news_item_id=item.id,
                rule_version=c.rule_version,
                rule=c.rule,
                event_type=c.event_type.value,
                direction=c.direction.value,
                severity=c.severity,
                noise=c.noise,
                noise_reason=c.noise_reason,
                importance_score=news.importance.score,
                importance_level=news.importance.level.value,
                factors=news.importance.factors,
                verification=news.verification.value,
                freshness=news.freshness.value,
                coverage=news.coverage,
                classified_at=news.stages["classified_at"],
            )
        )
        if cluster.last_verification != news.verification or cluster_is_new:
            # Status changes are appended, never overwritten (research-safe history).
            session.add(
                NewsClusterStatus(
                    cluster_id=cluster.id,
                    verification=news.verification.value,
                    importance_score=news.importance.score,
                    importance_level=news.importance.level.value,
                    source_count=len(cluster.item_ids),
                    recorded_at=news.stages["classified_at"],
                )
            )
    return True


def link_setup(
    session: Session,
    cluster_id: uuid.UUID,
    setup: ActiveSetupRef,
    match: EntityMatch,
    reason: str,
    now: datetime,
) -> bool:
    with session.begin():
        linked = session.scalar(
            _insert(session)(NewsSetupLink)
            .values(
                id=uuid.uuid4(),
                cluster_id=cluster_id,
                market_setup_id=setup.setup_id,
                symbol=setup.symbol,
                match_type=match.match_type.value,
                relevance=match.confidence,
                reason=reason[:200],
                setup_score_at_link=setup.score,
                linked_at=now,
            )
            .on_conflict_do_nothing(index_elements=["cluster_id", "market_setup_id"])
            .returning(NewsSetupLink.id)
        )
    return linked is not None


def claim_alert(
    session: Session,
    dedupe_key: str,
    kind: str,
    cluster_id: uuid.UUID,
    setup_id: uuid.UUID | None,
    symbol: str | None,
    now: datetime,
) -> bool:
    """Claim before sending: at most one Telegram message per dedupe key, ever."""
    with session.begin():
        claimed = session.scalar(
            _insert(session)(NewsAlert)
            .values(
                id=uuid.uuid4(),
                dedupe_key=dedupe_key,
                kind=kind,
                cluster_id=cluster_id,
                market_setup_id=setup_id,
                symbol=symbol,
                status="CLAIMED",
                claimed_at=now,
                latency_ms={},
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(NewsAlert.id)
        )
    return claimed is not None


def finish_alert(
    session: Session,
    dedupe_key: str,
    sent: bool,
    message_id: int | None,
    error: str | None,
    latency_ms: dict[str, float],
    now: datetime,
) -> None:
    with session.begin():
        session.execute(
            update(NewsAlert)
            .where(NewsAlert.dedupe_key == dedupe_key)
            .values(
                status="SENT" if sent else "FAILED",
                sent_at=now if sent else None,
                telegram_message_id=message_id,
                error=error,
                latency_ms=latency_ms,
            )
        )


def recent_urgent(session: Session, symbol: str, since: datetime) -> bool:
    return (
        session.scalar(
            select(NewsAlert.id)
            .where(
                NewsAlert.symbol == symbol,
                NewsAlert.kind.in_(("URGENT", "URGENT_PRELIMINARY")),
                NewsAlert.status == "SENT",
                NewsAlert.sent_at >= since,
            )
            .limit(1)
        )
        is not None
    )


@dataclass(frozen=True)
class NewsContext:
    """What was known at `as_of` about one relevant event, for alert rendering."""

    cluster_id: uuid.UUID
    title: str
    source: str
    source_type: int
    event_type: str
    direction: str
    level: str
    score: int
    verification: str
    match_type: str
    matched_symbol: str
    published_at: datetime
    received_at: datetime


def setup_news_context(
    session: Session,
    symbol: str,
    as_of: datetime,
    window: timedelta = timedelta(minutes=60),
    critical_window: timedelta = timedelta(hours=4),
    limit: int = 3,
) -> list[NewsContext]:
    """Top relevant events RECEIVED no later than `as_of` (causal: never future news)."""
    rows = session.execute(
        select(NewsItem, NewsClassification, NewsEntity)
        .join(NewsClassification, NewsClassification.news_item_id == NewsItem.id)
        .join(NewsEntity, NewsEntity.news_item_id == NewsItem.id)
        .where(
            NewsEntity.symbol == symbol,
            NewsItem.received_at <= as_of,
            NewsItem.received_at >= as_of - critical_window,
            NewsClassification.noise.is_(False),
        )
    ).all()
    best: dict[uuid.UUID, NewsContext] = {}
    for item, classification, entity in rows:
        received = _utc(item.received_at)
        level = classification.importance_level
        in_window = as_of - received <= (
            critical_window if level == ImpactLevel.CRITICAL else window
        )
        direct = entity.match_type in ("DIRECT_SYMBOL", "PROJECT_NAME", "ORGANIZATION")
        eligible = level in ("CRITICAL", "HIGH") or (level == "MEDIUM" and direct)
        if not (in_window and eligible and item.cluster_id):
            continue
        context = NewsContext(
            cluster_id=item.cluster_id,
            title=item.title,
            source=item.source,
            source_type=item.source_type,
            event_type=classification.event_type,
            direction=classification.direction,
            level=level,
            score=classification.importance_score,
            verification=classification.verification,
            match_type=entity.match_type,
            matched_symbol=entity.symbol,
            published_at=_utc(item.published_at),
            received_at=received,
        )
        current = best.get(item.cluster_id)
        if current is None or (context.source_type, -context.score) < (
            current.source_type,
            -current.score,
        ):
            best[item.cluster_id] = context  # primary source preferred per cluster
    ranked = sorted(
        best.values(),
        key=lambda c: (
            c.match_type not in ("DIRECT_SYMBOL", "PROJECT_NAME", "ORGANIZATION"),
            -c.score,
            -c.received_at.timestamp(),
        ),
    )
    return ranked[:limit]
