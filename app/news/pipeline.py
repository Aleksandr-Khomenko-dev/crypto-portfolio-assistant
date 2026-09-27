"""Event-driven news pipeline, independent of scanner / OI / scheduler cadence.

provider -> submit (bounded priority queue) -> workers: resolve, classify, cluster,
persist, link to active setups -> telegram queue -> dispatch. Every stage is
timestamped; news never changes a trading score and never invalidates a setup.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.news import repository as repo
from app.news.classify import (
    classify,
    freshness,
    importance,
    is_direct,
)
from app.news.cluster import Clusterer
from app.news.domain import (
    EntityMatch,
    ImpactLevel,
    NormalizedNewsItem,
    ProcessedNews,
    Verification,
)
from app.news.entities import Coverage, NewsEntityResolver, portfolio_pairs
from app.news.metrics import NewsMetrics
from app.news.providers.base import NewsProvider
from app.telegram.scanner import Mode

logger = logging.getLogger(__name__)
Sender = Callable[[str, int | None], Awaitable[list[int]]]  # (html, reply_to) -> ids

CRITICAL, HIGH, NORMAL, LOW = 0, 1, 2, 3


class BoundedPriorityQueue:
    """Bounded min-heap. When full, a more urgent job evicts the least urgent one;
    otherwise the new job is dropped. CRITICAL work is never displaced by lower."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._heap: list[tuple[int, int, object]] = []
        self._seq = itertools.count()
        self._ready = asyncio.Event()
        self.dropped = 0

    def qsize(self) -> int:
        return len(self._heap)

    def put_nowait(self, priority: int, payload: object) -> bool:
        entry = (priority, next(self._seq), payload)
        if len(self._heap) < self.maxsize:
            heapq.heappush(self._heap, entry)
            self._ready.set()
            return True
        worst = max(self._heap)
        self.dropped += 1  # exactly one job is lost either way
        if priority < worst[0]:
            self._heap.remove(worst)
            heapq.heapify(self._heap)
            heapq.heappush(self._heap, entry)
            return True
        return False

    async def get(self) -> tuple[int, object]:
        while not self._heap:
            self._ready.clear()
            await self._ready.wait()
        priority, _, payload = heapq.heappop(self._heap)
        return priority, payload

    def drain_urgent(self, max_priority: int) -> list[object]:
        urgent = [p for pr, _, p in sorted(self._heap) if pr <= max_priority]
        self._heap = [e for e in self._heap if e[0] > max_priority]
        heapq.heapify(self._heap)
        return urgent


class ActiveSetupIndex:
    """In-memory active setups by symbol. The DB stays the source of truth; the index
    reloads when marked dirty (setup persisted/changed) or when older than `max_age`."""

    def __init__(self, sessions: sessionmaker[Session], max_age: float = 60) -> None:
        self.sessions, self.max_age = sessions, max_age
        self._by_symbol: dict[str, list[repo.ActiveSetupRef]] = {}
        self._loaded_mono: float | None = None
        self._dirty = True
        self._lock = asyncio.Lock()

    def mark_dirty(self) -> None:
        self._dirty = True

    def _load(self) -> dict[str, list[repo.ActiveSetupRef]]:
        with self.sessions() as session:
            refs = repo.active_setups(session, datetime.now(UTC))
        grouped: dict[str, list[repo.ActiveSetupRef]] = {}
        for ref in refs:
            grouped.setdefault(ref.symbol, []).append(ref)
        return grouped

    async def get(self, symbol: str) -> list[repo.ActiveSetupRef]:
        async with self._lock:
            stale = (
                self._loaded_mono is None
                or time.monotonic() - self._loaded_mono > self.max_age
            )
            if self._dirty or stale:
                self._dirty = False
                self._by_symbol = await asyncio.to_thread(self._load)
                self._loaded_mono = time.monotonic()
        return list(self._by_symbol.get(symbol, []))


@dataclass
class TelegramJob:
    dedupe_key: str
    kind: str
    text: str
    cluster_id: uuid.UUID
    setup_id: uuid.UUID | None
    symbol: str | None
    reply_to: int | None
    news: ProcessedNews
    stages: dict[str, datetime] = field(default_factory=dict)


def ms(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds() * 1000, 1)


class NewsPipeline:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        coverage: Coverage,
        sender: Sender | None,
        *,
        mode: Mode = "LIVE",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings, self.sessions, self.coverage = settings, sessions, coverage
        self.resolver = NewsEntityResolver(coverage)
        self.sender, self.mode, self.clock = sender, mode, clock
        self.metrics = NewsMetrics(settings.news_latency_slo_ms)
        self.ingest = BoundedPriorityQueue(settings.news_queue_size)
        self.outbox = BoundedPriorityQueue(max(10, settings.news_queue_size // 10))
        self.index = ActiveSetupIndex(sessions)
        self.clusterer = Clusterer()
        self.providers: list[NewsProvider] = []
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_telegram_mono = 0.0
        self._cluster_locks: dict[object, asyncio.Lock] = {}

    # ---- ingestion -------------------------------------------------------------

    async def submit(self, item: NormalizedNewsItem) -> bool:
        """Provider callback. Never blocks: a full queue drops the least urgent job."""
        accepted_at = self.clock()
        self.metrics.counters["news_received"] += 1
        self.metrics.record("news_ingest_latency_ms", ms(accepted_at, item.received_at))
        upstream = ms(item.received_at, item.published_at)
        if upstream <= self.settings.news_alert_max_age_minutes * 60_000:
            # Fresh items only: a first poll's backlog (days-old notices) measures
            # archive age, not provider delivery latency.
            self.metrics.record("news_upstream_latency_ms", upstream)
        preliminary = classify(item)  # cheap regex pass decides queue priority only
        priority = (
            LOW
            if preliminary.noise
            else CRITICAL
            if preliminary.severity >= 90
            else HIGH
            if preliminary.severity >= 75
            else NORMAL
        )
        accepted = self.ingest.put_nowait(priority, (item, accepted_at))
        self.metrics.counters["news_queue_dropped"] = self.ingest.dropped
        return accepted

    # ---- processing ------------------------------------------------------------

    async def process(
        self, item: NormalizedNewsItem, accepted_at: datetime
    ) -> ProcessedNews | None:
        stages = {"received_at": item.received_at, "normalized_at": accepted_at}
        matches = self.resolver.resolve(item)
        classification = classify(item)
        coverage = {m.symbol: self.coverage.level(m.symbol) for m in matches}
        now = self.clock()
        self.clusterer.prune(now)
        live = {c.id for c in self.clusterer.clusters}
        self._cluster_locks = {
            k: v for k, v in self._cluster_locks.items() if k in live or v.locked()
        }
        cluster, is_new = self.clusterer.assign(
            item.id,
            item.url,
            item.source_type,
            classification.event_type,
            {m.symbol for m in matches},
            item.received_at,
        )
        status = cluster.verification
        news = ProcessedNews(
            item=item,
            matches=matches,
            classification=classification,
            importance=importance(
                item,
                classification,
                matches,
                coverage,
                status,
                freshness(item.published_at, now),
                novel=is_new,
            ),
            verification=status,
            freshness=freshness(item.published_at, now),
            coverage=coverage,
        )
        stages["classified_at"] = self.clock()
        news.stages = stages
        self.metrics.record(
            "news_classification_latency_ms", ms(stages["classified_at"], accepted_at)
        )
        if classification.noise:
            self.metrics.counters["news_filtered_noise"] += 1
        # Items of one cluster are stored in order, so a follow-up item can never
        # reference a cluster row that another worker has not committed yet.
        lock = self._cluster_locks.setdefault(cluster.id, asyncio.Lock())
        async with lock:
            stored = await asyncio.to_thread(self._store, news, cluster, is_new)
        if not stored:
            self.metrics.counters["news_items_deduplicated"] += 1
            return None
        previous = cluster.last_verification
        cluster.last_verification = status
        if news.importance.level in (ImpactLevel.CRITICAL, ImpactLevel.HIGH):
            self.metrics.counters["news_high_impact"] += 1
        await self._link_and_alert(news, cluster, previous)
        return news

    def _store(self, news, cluster, is_new) -> bool:
        with self.sessions() as session:
            return repo.store_processed(session, news, cluster, is_new)

    def _alertable(self, news: ProcessedNews) -> bool:
        if news.classification.noise:
            return False
        age = self.clock() - news.item.published_at
        limit = (
            self.settings.news_critical_max_age_minutes
            if news.importance.level == ImpactLevel.CRITICAL
            else self.settings.news_alert_max_age_minutes
        )
        return age <= timedelta(minutes=limit)  # no backlog alerts after a restart

    async def _link_and_alert(self, news: ProcessedNews, cluster, previous) -> None:
        now = self.clock()
        linked: list[tuple[repo.ActiveSetupRef, EntityMatch]] = []
        for match in news.matches:
            if match.confidence < self.settings.news_min_link_confidence:
                continue
            for setup in await self.index.get(match.symbol):
                reason = (
                    f"{match.symbol.removesuffix('USDT')} — "
                    f"{'прямая' if is_direct(match) else 'экосистема'} связь"
                )
                created = await asyncio.to_thread(
                    self._link, cluster.id, setup, match, reason, now
                )
                if created:
                    linked.append((setup, match))
                    self.metrics.counters["news_setup_links"] += 1
        news.stages["linked_at"] = self.clock()
        self.metrics.record(
            "news_setup_link_latency_ms",
            ms(news.stages["linked_at"], news.stages["classified_at"]),
        )
        if not self._alertable(news) or self.sender is None:
            return
        from app.telegram.news import format_setup_followup, format_urgent

        level = news.importance.level
        for setup, match in linked:
            direct = is_direct(match)
            if level in (ImpactLevel.CRITICAL, ImpactLevel.HIGH) or (
                level == ImpactLevel.MEDIUM and direct
            ):
                self._queue(
                    TelegramJob(
                        dedupe_key=f"FOLLOWUP:{cluster.id}:{setup.setup_id}",
                        kind="SETUP_FOLLOWUP",
                        text=format_setup_followup(news, setup, match, now, self.mode),
                        cluster_id=cluster.id,
                        setup_id=setup.setup_id,
                        symbol=setup.symbol,
                        reply_to=setup.telegram_message_id,
                        news=news,
                    ),
                    CRITICAL if level == ImpactLevel.CRITICAL else HIGH,
                )
        if not news.matches or level != ImpactLevel.CRITICAL:
            return
        best = news.matches[0]
        strong = best.confidence >= self.settings.news_min_link_confidence
        covered = news.coverage.get(best.symbol) != "UNCOVERED" or bool(linked)
        if strong and covered:
            confirmed = news.verification in (
                Verification.PRIMARY_CONFIRMED,
                Verification.MULTI_SOURCE_CONFIRMED,
            )
            upgraded = previous in (Verification.SINGLE_SOURCE, Verification.UNVERIFIED)
            kind = (
                "URGENT_CONFIRMED"
                if confirmed and upgraded
                else "URGENT"
                if confirmed
                else "URGENT_PRELIMINARY"
            )
            if kind != "URGENT_CONFIRMED" and await asyncio.to_thread(
                self._cooling_down, best.symbol, now
            ):
                self.metrics.counters["news_urgent_cooldown_skips"] += 1
                return
            affected = [s for s, _ in linked]
            self._queue(
                TelegramJob(
                    dedupe_key=f"{kind}:{cluster.id}",
                    kind=kind,
                    text=format_urgent(news, best, affected, now, kind, self.mode),
                    cluster_id=cluster.id,
                    setup_id=None,
                    symbol=best.symbol,
                    reply_to=None,
                    news=news,
                ),
                CRITICAL,
            )

    def _link(self, cluster_id, setup, match, reason, now) -> bool:
        with self.sessions() as session:
            return repo.link_setup(session, cluster_id, setup, match, reason, now)

    def _cooling_down(self, symbol: str, now: datetime) -> bool:
        since = now - timedelta(minutes=self.settings.news_urgent_cooldown_minutes)
        with self.sessions() as session:
            return repo.recent_urgent(session, symbol, since)

    def _queue(self, job: TelegramJob, priority: int) -> None:
        job.stages = dict(job.news.stages)
        if not self.outbox.put_nowait(priority, job):
            self.metrics.counters["news_telegram_dropped"] += 1

    # ---- telegram --------------------------------------------------------------

    async def dispatch(self, job: TelegramJob) -> bool:
        now = self.clock()
        claimed = await asyncio.to_thread(self._claim, job, now)
        if not claimed:
            self.metrics.counters["news_alerts_duplicate"] += 1
            return False
        # Respect Telegram pacing for one chat without batching behind other work.
        wait = self.settings.scanner_telegram_interval_seconds - (
            time.monotonic() - self._last_telegram_mono
        )
        if wait > 0:
            await asyncio.sleep(wait)
        job.stages["telegram_dispatch_started_at"] = self.clock()
        ids, error = [], None
        try:
            assert self.sender is not None
            ids = await asyncio.wait_for(
                self.sender(job.text, job.reply_to),
                timeout=self.settings.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - failed delivery is recorded, not retried in a loop
            error = type(exc).__name__
            logger.warning(
                "News Telegram delivery failed kind=%s error=%s", job.kind, error
            )
        self._last_telegram_mono = time.monotonic()
        done = job.stages["telegram_dispatch_completed_at"] = self.clock()
        latency = {
            "ingest_to_classification_ms": ms(
                job.stages["classified_at"], job.stages["received_at"]
            ),
            "classification_to_link_ms": ms(
                job.stages["linked_at"], job.stages["classified_at"]
            ),
            "link_to_telegram_ms": ms(done, job.stages["linked_at"]),
            "telegram_send_ms": ms(done, job.stages["telegram_dispatch_started_at"]),
            "total_event_to_telegram_ms": ms(done, job.stages["received_at"]),
        }
        if ids:
            self.metrics.record("news_telegram_latency_ms", latency["telegram_send_ms"])
            self.metrics.record(
                "news_total_latency_ms", latency["total_event_to_telegram_ms"]
            )
            self.metrics.counters[
                "urgent_alerts" if job.kind.startswith("URGENT") else "news_followups"
            ] += 1
        await asyncio.to_thread(
            self._finish, job, bool(ids), ids[0] if ids else None, error, latency
        )
        return bool(ids)

    def _claim(self, job: TelegramJob, now: datetime) -> bool:
        with self.sessions() as session:
            return repo.claim_alert(
                session,
                job.dedupe_key,
                job.kind,
                job.cluster_id,
                job.setup_id,
                job.symbol,
                now,
            )

    def _finish(self, job, sent, message_id, error, latency) -> None:
        with self.sessions() as session:
            repo.finish_alert(
                session, job.dedupe_key, sent, message_id, error, latency, self.clock()
            )

    # ---- lifecycle -------------------------------------------------------------

    async def _ingest_worker(self) -> None:
        while True:
            _, payload = await self.ingest.get()
            item, accepted_at = cast(tuple[NormalizedNewsItem, datetime], payload)
            try:
                await self.process(item, accepted_at)
            except Exception as exc:  # noqa: BLE001 - one bad item never stops the pipeline
                self.metrics.counters["news_processing_errors"] += 1
                logger.warning("News processing failed error=%s", type(exc).__name__)

    async def _telegram_worker(self) -> None:
        while True:
            _, job = await self.outbox.get()
            await self.dispatch(cast(TelegramJob, job))

    async def start(self, providers: list[NewsProvider]) -> None:
        with self.sessions() as session:
            self.coverage.portfolio = await asyncio.to_thread(portfolio_pairs, session)
        self.providers = providers
        for provider in providers:
            self.metrics.providers[provider.name] = provider.health
        self._tasks = [
            *(asyncio.create_task(p.run(self.submit, self._stop)) for p in providers),
            *(
                asyncio.create_task(self._ingest_worker())
                for _ in range(self.settings.news_workers)
            ),
            asyncio.create_task(self._telegram_worker()),
        ]

    async def stop(self, drain_timeout: float = 10) -> None:
        """Stop providers and workers; already-accepted CRITICAL items are still
        processed and dispatched (bounded by `drain_timeout`)."""
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        urgent = self.ingest.drain_urgent(CRITICAL)

        async def drain() -> None:
            for payload in urgent:
                item, accepted_at = cast(tuple[NormalizedNewsItem, datetime], payload)
                await self.process(item, accepted_at)
            for job in self.outbox.drain_urgent(CRITICAL):
                await self.dispatch(cast(TelegramJob, job))

        try:
            await asyncio.wait_for(drain(), timeout=drain_timeout)
        except TimeoutError:
            logger.warning("News shutdown drain timed out")
        for provider in self.providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    def snapshot(self) -> dict:
        data = self.metrics.snapshot(self.ingest.qsize())
        data["news_queue_dropped"] = self.ingest.dropped
        data["telegram_queue_depth"] = self.outbox.qsize()
        return data
