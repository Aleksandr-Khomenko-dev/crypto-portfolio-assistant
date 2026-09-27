"""Event-driven news pipeline: DB-backed behaviour, transports, latency and isolation."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.scanner_models import (
    MarketSetup,
    NewsAlert,
    NewsClassification,
    NewsItem,
    NewsSetupLink,
    ScannerSnapshot,
)
from app.db.session import get_session_factory
from app.news.domain import NormalizedNewsItem, SourceType, Transport
from app.news.entities import load_coverage
from app.news.pipeline import CRITICAL, LOW, NORMAL, BoundedPriorityQueue, NewsPipeline
from app.news.providers.base import PollingProvider, StreamingProvider
from app.news.repository import setup_news_context
from app.services.scanner_service import (
    ScannerRuntime,
    ScannerService,
    lifecycle_tick,
)
from tests.scanner_fixtures import FixtureFutures
from tests.test_scanner_service import save_ready

LIVE = {"telegram_bot_token": "123:abc", "scanner_telegram_chat_id": "777"}


def news(
    title,
    *,
    source_type=SourceType.OFFICIAL_PRIMARY,
    age=timedelta(seconds=5),
    item_id=None,
    provider="wire",
    url=None,
):
    now = datetime.now(UTC)
    return NormalizedNewsItem(
        id=f"{provider}:{item_id or title}",
        provider=provider,
        provider_item_id=item_id or title,
        source="Test Source",
        source_type=source_type,
        title=title,
        url=url,
        published_at=now - age,
        received_at=now,
        raw_payload_hash="h",
    )


class Recorder:
    """Fake Telegram: records (text, reply_to) and returns a message id."""

    def __init__(self):
        self.sent, self.event = [], asyncio.Event()

    async def __call__(self, text, reply_to):
        self.sent.append((text, reply_to))
        self.event.set()
        return [1000 + len(self.sent)]


def pipeline_for(sqlite_database_url, sender=None, **settings):
    config = Settings(scanner_telegram_interval_seconds=0, **settings)
    return NewsPipeline(
        config, get_session_factory(sqlite_database_url), load_coverage(), sender
    )


async def until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


@pytest.fixture()
def active_setup(session):
    """A READY LONG AAAUSDT setup whose first alert was message 4242."""
    _, _, _, row = save_ready(session)
    row.notified_data = {"telegram_message_id": 4242, "score": row.score}
    session.commit()
    return row


# --- event-driven processing ------------------------------------------------------


async def test_push_news_updates_active_setup_immediately_without_rescan(
    session, sqlite_database_url, active_setup
):
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    await pipeline.start([])  # no scanner, no candles, no OI: news only
    score_before = active_setup.score
    try:
        received = time.monotonic()
        await pipeline.submit(news("$AAA bridge exploit drains funds"))
        await until(lambda: len(sender.sent) >= 2)
        elapsed_ms = (time.monotonic() - received) * 1000
    finally:
        await pipeline.stop()
    kinds = {text.split("\n")[0] for text, _ in sender.sent}
    assert "📰 <b>НОВЫЙ КОНТЕКСТ ДЛЯ АКТИВНОГО СИГНАЛА</b>" in kinds
    assert "🚨 <b>СРОЧНАЯ НОВОСТЬ</b>" in kinds
    followup = next(t for t in sender.sent if "НОВЫЙ КОНТЕКСТ" in t[0])
    assert followup[1] == 4242  # threaded under the original setup alert
    assert f"<b>{score_before} / 100</b> (не меняется из-за новости)" in followup[0]
    session.expire_all()
    assert session.get(MarketSetup, active_setup.id).score == score_before
    assert session.get(MarketSetup, active_setup.id).lifecycle == "ACTIVE"
    assert session.scalar(select(func.count()).select_from(NewsSetupLink)) == 1
    alerts = session.scalars(select(NewsAlert)).all()
    assert {a.status for a in alerts} == {"SENT"}
    assert all(a.latency_ms["total_event_to_telegram_ms"] >= 0 for a in alerts)
    assert elapsed_ms < 3000
    summary = pipeline.snapshot()["latency"]["news_total_latency_ms"]
    assert summary["count"] == 2 and summary["p95"] < 3000


async def test_duplicate_event_is_stored_once_and_never_resent(
    session, sqlite_database_url, active_setup
):
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    await pipeline.start([])
    try:
        item = news("$AAA bridge exploit drains funds", item_id="same")
        await pipeline.submit(item)
        await until(lambda: len(sender.sent) >= 2)
        await pipeline.submit(item)  # e.g. replay after reconnect / poll overlap
        await until(lambda: pipeline.metrics.counters["news_items_deduplicated"] == 1)
    finally:
        await pipeline.stop()
    assert len(sender.sent) == 2
    assert session.scalar(select(func.count()).select_from(NewsItem)) == 1


async def test_old_backlog_and_noise_are_stored_but_not_alerted(
    session, sqlite_database_url, active_setup
):
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    await pipeline.start([])
    try:
        await pipeline.submit(
            news("$AAA bridge exploit drains funds", age=timedelta(hours=6))
        )
        await pipeline.submit(news("$AAA price prediction: could reach $5"))
        await until(
            lambda: session.scalar(select(func.count()).select_from(NewsItem)) == 2
        )
        await asyncio.sleep(0.1)
    finally:
        await pipeline.stop()
    assert sender.sent == []
    noise = session.scalar(select(NewsClassification).where(NewsClassification.noise))
    assert noise.noise_reason == "price_prediction"


async def test_preliminary_then_confirmed_and_cooldown(session, sqlite_database_url):
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    await pipeline.start([])
    try:
        await pipeline.submit(
            news(
                "$ETH bridge exploit reported",
                source_type=SourceType.NEWSWIRE,
                item_id="w",
            )
        )
        await until(lambda: len(sender.sent) == 1)
        assert sender.sent[0][0].startswith("⚠️ <b>ПЕРВИЧНОЕ СООБЩЕНИЕ</b>")
        assert "SINGLE_SOURCE" in sender.sent[0][0]
        await pipeline.submit(
            news("$ETH bridge exploit confirmed by Ethereum Foundation", item_id="o")
        )
        await until(lambda: len(sender.sent) == 2)
        assert sender.sent[1][0].startswith("✅ <b>НОВОСТЬ ПОДТВЕРЖДЕНА</b>")
        assert "PRIMARY_CONFIRMED" in sender.sent[1][0]
        # A different critical event for the same symbol inside the cooldown: skipped.
        await pipeline.submit(news("$ETH network halted", item_id="h"))
        await until(
            lambda: pipeline.metrics.counters["news_urgent_cooldown_skips"] == 1
        )
    finally:
        await pipeline.stop()
    assert len(sender.sent) == 2
    # Both the preliminary and the confirmation are preserved (never rewritten).
    kinds = sorted(session.scalars(select(NewsAlert.kind)).all())
    assert kinds == ["URGENT_CONFIRMED", "URGENT_PRELIMINARY"]


# --- queue ----------------------------------------------------------------------


async def test_bounded_priority_queue_backpressure_and_critical_first():
    queue = BoundedPriorityQueue(3)
    assert queue.put_nowait(LOW, "low-1") and queue.put_nowait(LOW, "low-2")
    assert queue.put_nowait(NORMAL, "normal")
    assert queue.put_nowait(CRITICAL, "critical")  # evicts the least urgent job
    assert not queue.put_nowait(LOW, "low-3")  # full of more urgent work: rejected
    assert queue.dropped == 2 and queue.qsize() == 3
    assert (await queue.get())[1] == "critical"
    assert (await queue.get())[1] == "normal"


async def test_graceful_shutdown_processes_already_accepted_critical_items(
    session, sqlite_database_url, active_setup
):
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    # No workers running: items sit in the queue when shutdown begins.
    await pipeline.submit(news("$AAA bridge exploit drains funds", item_id="c"))
    await pipeline.submit(news("$AAA minor update", item_id="l"))
    await pipeline.stop()
    assert any("СРОЧНАЯ НОВОСТЬ" in text for text, _ in sender.sent)
    stored = session.scalars(select(NewsItem.provider_item_id)).all()
    assert stored == ["c"]  # only CRITICAL work is drained on shutdown


# --- transports -----------------------------------------------------------------


class FakeStream(StreamingProvider):
    name = "fake_stream"
    transport = Transport.WEBSOCKET

    def __init__(self, sessions):
        super().__init__(
            stale_after_seconds=0.3, heartbeat_seconds=0.05, max_backoff=0.05
        )
        self.sessions = list(sessions)

    async def connect(self):
        script = self.sessions.pop(0) if self.sessions else ["silence"]
        for step in script:
            if step == "drop":
                raise ConnectionError("socket closed")
            if step == "silence":
                await asyncio.sleep(3600)  # stale connection: watchdog must recycle it
            yield step

    def parse(self, message, received_at):
        item = news(f"stream {message}", item_id=message, provider=self.name)
        return [item.model_copy(update={"received_at": received_at})]


async def test_stream_reconnects_dedupes_replays_and_detects_stale():
    received = []

    async def submit(item):
        received.append(item.provider_item_id)
        return True

    provider = FakeStream([["m1", "drop"], ["m1", "m2", "silence"], ["m3"]])
    stop = asyncio.Event()
    task = asyncio.create_task(provider.run(submit, stop))
    await until(lambda: "m3" in received, timeout=5)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert received == ["m1", "m2", "m3"]  # replayed m1 after reconnect is dropped
    assert provider.health.reconnects >= 2
    assert provider.health.stale_events >= 1
    assert provider.health.errors >= 1


class Broken(PollingProvider):
    name = "broken"
    transport = Transport.REST_POLL

    async def poll(self):
        raise RuntimeError("provider down")


class Healthy(PollingProvider):
    name = "healthy"
    transport = Transport.REST_POLL

    async def poll(self):
        return [
            news("$AAA bridge exploit drains funds", item_id="ok", provider=self.name)
        ]


async def test_provider_outage_is_isolated_and_reported(
    session, sqlite_database_url, active_setup
):
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    broken, healthy = Broken(interval=0.05), Healthy(interval=0.05)
    broken.max_backoff = 0.1
    await pipeline.start([broken, healthy])
    try:
        await until(lambda: len(sender.sent) >= 1)
        await until(lambda: broken.health.errors >= 2)
        status = pipeline.snapshot()["providers"]
    finally:
        await pipeline.stop()
    assert status["broken"]["slo_status"] == "STALE"
    assert status["healthy"]["slo_status"] in ("REALTIME_OK", "DEGRADED")
    assert status["healthy"]["transport"] == "REST_POLL"


def test_polling_provider_detects_staleness():
    provider = Healthy(interval=30)
    assert provider.health.is_stale()  # never succeeded
    provider.health.success()
    assert not provider.health.is_stale()
    provider.health.last_success_mono -= provider.health.stale_after_seconds + 1
    assert provider.health.is_stale()


# --- causality and scanner isolation --------------------------------------------


async def test_setup_news_context_never_uses_future_news(
    session, sqlite_database_url, active_setup
):
    pipeline = pipeline_for(sqlite_database_url)
    item = news("$AAA bridge exploit drains funds", item_id="t")
    await pipeline.process(item, item.received_at)
    before = setup_news_context(
        session, "AAAUSDT", item.received_at - timedelta(seconds=1)
    )
    after = setup_news_context(
        session, "AAAUSDT", item.received_at + timedelta(seconds=1)
    )
    assert before == [] and len(after) == 1
    assert after[0].level == "CRITICAL" and after[0].match_type == "DIRECT_SYMBOL"


async def test_at_most_three_events_per_setup(
    session, sqlite_database_url, active_setup
):
    pipeline = pipeline_for(sqlite_database_url)
    titles = [
        "exploit drains",
        "network halted",
        "stablecoin lost its peg",
        "hacked again",
    ]
    for index, title in enumerate(titles):
        # Distinct event types so each is its own cluster.
        item = news(f"$AAA {title}", item_id=str(index))
        await pipeline.process(item, item.received_at)
    context = setup_news_context(
        session, "AAAUSDT", datetime.now(UTC) + timedelta(seconds=1)
    )
    assert len(context) == 3


async def test_trading_scores_identical_with_and_without_news(
    session, sqlite_database_url
):
    now = datetime(2026, 9, 24, 12, 7, tzinfo=UTC)
    runtime = ScannerRuntime(FixtureFutures(), Settings())
    await ScannerService(session, runtime).run(now)
    first = {
        (s.symbol, s.long_score, s.short_score)
        for s in session.scalars(select(ScannerSnapshot))
    }
    pipeline = pipeline_for(sqlite_database_url)
    for index, title in enumerate(["$AAA exploit drains", "$BBB network halted"]):
        item = news(title, item_id=str(index))
        await pipeline.process(item, item.received_at)
    session.execute(ScannerSnapshot.__table__.delete())
    session.commit()
    await ScannerService(session, ScannerRuntime(FixtureFutures(), Settings())).run(now)
    second = {
        (s.symbol, s.long_score, s.short_score)
        for s in session.scalars(select(ScannerSnapshot))
    }
    assert first == second  # news is context only


async def test_scanner_works_when_news_disabled_and_tables_empty(session):
    runtime = ScannerRuntime(FixtureFutures(), Settings(news_enabled=False, **LIVE))
    run = await ScannerService(session, runtime).run()
    assert run.status == "COMPLETED"


class SlowSecondSymbol(FixtureFutures):
    def __init__(self):
        super().__init__()
        self.bbb_done_at = None

    async def candles(self, symbol, timeframe, now):
        if symbol == "BBBUSDT":
            await asyncio.sleep(0.4)
            self.bbb_done_at = time.monotonic()
        return await super().candles(symbol, timeframe, now)


async def test_alert_delivery_runs_as_each_symbol_is_persisted(session, monkeypatch):
    calls = []

    async def deliver(*args, **kwargs):
        calls.append(time.monotonic())
        return 0

    monkeypatch.setattr("app.services.scanner_service.deliver_notifications", deliver)
    provider = SlowSecondSymbol()
    settings = Settings(scanner_use_btc_context=False, **LIVE)
    await ScannerService(session, ScannerRuntime(provider, settings)).run()
    # A delivery pass happened after AAAUSDT was persisted, before BBBUSDT finished:
    # alerts no longer wait for the whole universe (or for outcome tracking).
    assert calls and calls[0] < provider.bbb_done_at


async def test_lifecycle_tick_expires_and_notifies_between_scans(session, monkeypatch):
    settings, _, _, row = save_ready(session, **LIVE)
    row.notified_data = {
        "score": row.score,
        "state": row.state,
        "readiness": "READY",
        "lifecycle": "ACTIVE",
    }
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    sent = AsyncMock(return_value=[99])
    monkeypatch.setattr(
        "app.services.telegram_service.TelegramService.send_scanner", sent
    )
    runtime = ScannerRuntime(FixtureFutures(), settings)
    listener = []
    runtime.setup_listeners.append(lambda: listener.append(1))
    assert await lifecycle_tick(runtime, session) == 1
    session.refresh(row)
    assert row.lifecycle == "EXPIRED" and listener
    assert "СЦЕНАРИЙ ИСТЁК" in sent.call_args.args[1]


# --- benchmark ------------------------------------------------------------------


async def test_benchmark_internal_latency_for_active_setup(
    session, sqlite_database_url, active_setup, capsys
):
    """Synthetic HIGH events for an active setup; fake Telegram answers instantly, so
    this measures OUR processing only (receipt -> dispatch completed)."""
    sender = Recorder()
    pipeline = pipeline_for(sqlite_database_url, sender)
    await pipeline.start([])
    try:
        for index in range(30):
            # Same type + symbol within 6 h would (correctly) merge into one cluster
            # and send one follow-up; the benchmark needs 30 distinct events.
            pipeline.clusterer.clusters.clear()
            await pipeline.submit(
                news(
                    f"$AAA delisting notice {index}",
                    item_id=f"b{index}",
                    url=f"https://example.invalid/{index}",
                )
            )
            await until(lambda n=index: len(sender.sent) > n, timeout=5)
    finally:
        await pipeline.stop()
    total = pipeline.snapshot()["latency"]["news_total_latency_ms"]
    with capsys.disabled():
        print(f"\nnews internal latency (fake Telegram): {total}")
    assert total["count"] >= 30 and total["p95"] < 3000
