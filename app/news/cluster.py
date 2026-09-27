"""Cross-source event clustering (deterministic). One real event -> one cluster."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.news.classify import verification
from app.news.domain import EventType, SourceType, Verification

WINDOW = timedelta(hours=6)


@dataclass
class ClusterState:
    id: uuid.UUID
    event_type: EventType
    symbols: set[str]
    first_received_at: datetime
    canonical_item_id: str
    canonical_url: str | None
    canonical_source_type: SourceType
    source_types: list[SourceType] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    urls: set[str] = field(default_factory=set)
    last_verification: Verification | None = None

    @property
    def verification(self) -> Verification:
        return verification(self.source_types)


class Clusterer:
    """Same event type + overlapping symbols (or identical URL) within 6 hours.

    The canonical source is the highest-priority one (official primary first); every
    supporting source stays recorded on the cluster.
    """

    def __init__(self) -> None:
        self.clusters: list[ClusterState] = []

    def prune(self, now: datetime) -> None:
        self.clusters = [
            c for c in self.clusters if now - c.first_received_at <= WINDOW
        ]

    def assign(
        self,
        item_id: str,
        url: str | None,
        source_type: SourceType,
        event_type: EventType,
        symbols: set[str],
        received_at: datetime,
    ) -> tuple[ClusterState, bool]:
        for cluster in self.clusters:
            same_url = url is not None and url in cluster.urls
            same_event = (
                cluster.event_type == event_type
                and bool(symbols & cluster.symbols)
                and abs(received_at - cluster.first_received_at) <= WINDOW
            )
            if same_url or same_event:
                if item_id not in cluster.item_ids:
                    cluster.item_ids.append(item_id)
                    cluster.source_types.append(source_type)
                cluster.symbols |= symbols
                if url:
                    cluster.urls.add(url)
                if source_type < cluster.canonical_source_type:
                    cluster.canonical_item_id, cluster.canonical_url = item_id, url
                    cluster.canonical_source_type = source_type
                return cluster, False
        cluster = ClusterState(
            id=uuid.uuid4(),
            event_type=event_type,
            symbols=set(symbols),
            first_received_at=received_at,
            canonical_item_id=item_id,
            canonical_url=url,
            canonical_source_type=source_type,
            source_types=[source_type],
            item_ids=[item_id],
            urls={url} if url else set(),
        )
        self.clusters.append(cluster)
        return cluster, True
