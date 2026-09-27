"""Deterministic collector timing, persistence, overlap and causal coverage regressions."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.analytics.derivatives import normalize_derivatives
from app.analytics.open_interest import (
    OIObservation,
    coverage,
    history_points,
    next_boundary,
)
from app.config import Settings
from app.db.scanner_models import OpenInterestSnapshot
from app.db.session import get_session_factory
from app.providers.bingx_futures import BingXFuturesProvider
from app.scanner.open_interest_repository import load_history, record
from app.scheduler.jobs import build_scheduler
from app.services.oi_collector_service import OICollectorService
from app.services.scanner_service import ScannerRuntime, ScannerService
from tests.scanner_fixtures import FixtureFutures
from tests.test_open_interest import STEP, B, bars_until, derivatives


class SamplingExchange(FixtureFutures):
    exchange = "BINGX"
    publishes_oi_history = False

    def __init__(self, count=66, fail=(), offset=1):
        super().__init__()
        self.count, self.fail, self.offset = count, fail, offset
        self.active = self.peak = self.calls = 0
        self.block = None

    async def contracts(self):
        template = (await super().contracts())[0]
        return [
            template.model_copy(
                update={"symbol": f"COIN{i}USDT", "base_asset": f"COIN{i}"}
            )
            for i in range(self.count)
        ]

    async def tickers(self):
        template = (await super().tickers())["AAAUSDT"]
        return {
            f"COIN{i}USDT": template.model_copy(update={"symbol": f"COIN{i}USDT"})
            for i in range(self.count)
        }

    @asynccontextmanager
    async def oi_collection_priority(self):
        yield

    async def oi_mark_prices(self, boundary):
        return dict.fromkeys(await self.tickers(), Decimal(100))

    async def oi_snapshot(self, symbol, boundary, mark):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.block is not None:
                await self.block.wait()
            await asyncio.sleep(0)
            if symbol in self.fail:
                raise RuntimeError("fixture")
            return OIObservation(
                boundary,
                boundary + timedelta(seconds=self.offset),
                Decimal(100),
                Decimal(10000),
            )
        finally:
            self.active -= 1

    async def funding(self, symbol, now):
        return await super().derivatives(symbol, now)

    async def derivatives(self, *args):
        raise AssertionError("Scanner must not fetch current OI")


def collector(url, provider=None, clock=None):
    runtime = ScannerRuntime(
        provider or SamplingExchange(), Settings(scanner_use_btc_context=False)
    )
    return OICollectorService(
        runtime,
        get_session_factory(url),
        clock=clock or (lambda: B + timedelta(seconds=60)),
    )


@pytest.mark.parametrize(
    "minute,expected",
    [
        (0, 15),
        (7, 15),
        (14, 15),
        (15, 30),
        (29, 30),
        (30, 45),
        (44, 45),
        (45, 0),
        (59, 0),
    ],
)
def test_next_boundary_and_arbitrary_restart(minute, expected):
    now = B.replace(minute=minute, second=0)
    next_ = next_boundary(now)
    assert next_.minute == expected and next_.second == 0
    assert now < next_ <= now + STEP


def test_scheduler_uses_clock_cron_and_no_restart_catchup():
    scheduler = build_scheduler(Settings())
    job = scheduler.get_job("oi-collector")
    assert job.max_instances == 1 and job.coalesce
    for minute in (0, 15, 30, 45):
        now = B.replace(minute=minute) + timedelta(seconds=1)
        assert job.trigger.get_next_fire_time(None, now) == next_boundary(now)
    assert job.next_run_time > datetime.now(UTC) - timedelta(seconds=1)
    assert (
        build_scheduler(Settings(scanner_provider="binance")).get_job("oi-collector")
        is None
    )


async def test_full_66_symbol_batch_bounded_concurrency_and_duplicates(
    sqlite_database_url,
):
    service = collector(sqlite_database_url)
    first = await service.collect(B)
    assert first["oi_collection_requested"] == first["oi_collection_successful"] == 66
    assert first["oi_snapshots_inserted"] == 66
    assert first["oi_collection_failed"] == first["oi_observations_unaligned"] == 0
    assert service.runtime.provider.peak == 5
    again = await service.collect(B)
    assert (
        again["oi_collection_duplicates"] == 66 and again["oi_snapshots_inserted"] == 0
    )
    assert again["oi_coverage"]["15m"]["availability_rate"] == 0


async def test_partial_failures_and_unaligned_observations(sqlite_database_url):
    result = await collector(
        sqlite_database_url, SamplingExchange(fail={"COIN0USDT", "COIN1USDT"})
    ).collect(B)
    assert (
        result["oi_collection_successful"] == 64 and result["oi_collection_failed"] == 2
    )
    assert result["status"] == "PARTIAL"
    late = collector(
        sqlite_database_url,
        SamplingExchange(offset=151),
        clock=lambda: B + timedelta(seconds=152),
    )
    assert (await late.collect(B))["status"] == "SKIPPED"
    # Returned stale/future timestamps must not be silently relabelled.
    unaligned = await collector(
        sqlite_database_url, SamplingExchange(offset=-151)
    ).collect(B)
    assert unaligned["oi_observations_unaligned"] == 66
    assert unaligned["oi_collection_successful"] == 0


async def test_overlapping_collectors_atomic_sqlite_idempotency(sqlite_database_url):
    first, second = collector(sqlite_database_url), collector(sqlite_database_url)
    results = await asyncio.gather(first.collect(B), second.collect(B))
    assert sum(r["oi_snapshots_inserted"] for r in results) == 66
    assert sum(r["oi_collection_duplicates"] for r in results) == 66
    assert all(r["oi_collection_successful"] == 66 for r in results)


async def test_collector_runs_while_scanner_network_is_blocked(sqlite_database_url):
    provider = SamplingExchange(count=2)
    service = collector(sqlite_database_url, provider)
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.candles

    async def stalled(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    provider.candles = stalled
    with get_session_factory(sqlite_database_url)() as session:
        scan = asyncio.create_task(
            ScannerService(session, service.runtime).run(B + timedelta(seconds=60))
        )
        await entered.wait()
        result = await asyncio.wait_for(service.collect(B), timeout=5)
        assert result["oi_snapshots_inserted"] == 2
        assert not scan.done()
        release.set()
        assert (await scan).status == "COMPLETED"
    assert provider.calls == 2  # no extra OI calls during scoring


async def test_graceful_shutdown_cancels_collection_before_provider_close(
    sqlite_database_url,
):
    provider = SamplingExchange()
    provider.block = asyncio.Event()
    service = collector(sqlite_database_url, provider)
    task = asyncio.create_task(service.collect(B))
    while provider.active == 0:
        await asyncio.sleep(0)
    assert (await service.collect(B))["status"] == "SKIPPED"
    await service.runtime.aclose()
    assert task.cancelled() and provider.closed and provider.active == 0
    assert service.runtime.oi_task is None
    assert service.runtime.oi_last_collection["status"] == "CANCELLED"


def test_coverage_exact_horizons_missing_and_future_observations():
    def point(bucket, observed=None):
        return OIObservation(bucket, observed or bucket, Decimal(100))

    history = {
        "A": {
            t: point(t)
            for t in (B, B - STEP, B - timedelta(hours=1), B - timedelta(hours=4))
        },
        "B": {B: point(B), B - STEP: point(B - STEP)},
        "C": {B: point(B, B + timedelta(seconds=60)), B - STEP: point(B - STEP)},
    }
    result = coverage(history, ["A", "B", "C", "D"], B, B)
    assert [result[h]["availability_rate"] for h in ("15m", "1h", "4h")] == [
        0.5,
        0.25,
        0.25,
    ]
    assert coverage({}, [], B, B)["15m"]["availability_rate"] == 0
    raw = derivatives(history_points(history["C"]), B)
    normalized = normalize_derivatives(raw, bars_until(B), Decimal(".001"), B)
    assert normalized.oi_change_pct is None
    assert normalized.oi_change_by_horizon["15m"] is None


async def test_sqlite_upsert_closest_wins_under_competing_writers(sqlite_database_url):
    factory = get_session_factory(sqlite_database_url)

    def write(offset):
        with factory.begin() as session:
            return record(
                session,
                "BINGX",
                "A",
                OIObservation(B, B + timedelta(seconds=offset), Decimal(offset)),
                B,
            )

    await asyncio.gather(*(asyncio.to_thread(write, n) for n in [120, 2, 40, 10]))
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(OpenInterestSnapshot)) == 1
        )
        assert load_history(session, "BINGX", ["A"], B)["A"][B].open_interest == 2


@respx.mock
async def test_snapshot_uses_same_limiter_and_refreshes_preboundary_cache():
    provider = BingXFuturesProvider(Settings())
    provider._budget.acquire = AsyncMock()
    route = respx.get(
        "https://open-api.bingx.com/openApi/swap/v2/quote/openInterest"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"openInterest": "1000", "time": int(B.timestamp() * 1000)},
            },
        )
    )
    try:
        await provider.oi_snapshot("BTCUSDT", B, Decimal(100))
        await provider.oi_snapshot("BTCUSDT", B, Decimal(100))
        assert route.call_count == 1 and provider._budget.acquire.await_count == 1
        key = next(iter(provider._cache))
        cached = provider._cache[key]
        provider._cache[key] = (cached[0], cached[1], B - timedelta(seconds=1))
        await provider.oi_snapshot("BTCUSDT", B, Decimal(100))
        assert route.call_count == 2 and provider._budget.acquire.await_count == 2
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "offset,price,expected",
    [
        (0, "100", True),
        (-1, "100", False),
        (61, "100", False),
        (1, "NaN", False),
        (1, "-1", False),
    ],
)
def test_mark_feed_causal_validation(offset, price, expected):
    import json

    from app.providers.bingx_mark_prices import parse_mark

    payload = {
        "dataType": "BTC-USDT@markPrice",
        "data": {
            "e": "markPriceUpdate",
            "E": int((B + timedelta(seconds=offset)).timestamp() * 1000),
            "s": "BTC-USDT",
            "p": price,
        },
    }
    assert (
        parse_mark(json.dumps(payload), B, B + timedelta(seconds=60)) is not None
    ) == expected
    assert parse_mark('{"id":"ack"}', B, B) is None


async def test_mark_feed_only_subscribes_marks_shares_budget_and_handles_ping(
    monkeypatch,
):
    import gzip
    import json
    from types import SimpleNamespace

    import aiohttp

    from app.providers.bingx_mark_prices import collect_mark_prices

    sent = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, data):
            sent.append(data)

        async def send_str(self, data):
            sent.append(data)

        def __aiter__(self):
            return self.messages()

        async def messages(self):
            for content in [
                "Ping",
                json.dumps(
                    {
                        "dataType": "BTC-USDT@markPrice",
                        "data": {
                            "e": "markPriceUpdate",
                            "E": int(B.timestamp() * 1000),
                            "s": "BTC-USDT",
                            "p": "100",
                        },
                    }
                ),
            ]:
                yield SimpleNamespace(
                    type=aiohttp.WSMsgType.BINARY, data=gzip.compress(content.encode())
                )

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            return Socket()

    monkeypatch.setattr("app.providers.bingx_mark_prices.aiohttp.ClientSession", Client)
    budget = SimpleNamespace(acquire=AsyncMock())
    assert await collect_mark_prices("wss://test", ["BTC-USDT"], B, budget, 1) == {
        "BTC-USDT": Decimal(100)
    }
    assert budget.acquire.await_count == 2
    assert "Pong" in sent
    assert [s["dataType"] for s in sent if isinstance(s, dict)] == [
        "BTC-USDT@markPrice"
    ]


@respx.mock
async def test_priority_gate_does_not_deadlock_an_existing_cache_miss():
    provider = BingXFuturesProvider(Settings())
    entered, release = asyncio.Event(), asyncio.Event()

    async def budget(_):
        entered.set()
        await release.wait()

    provider._budget.acquire = budget
    route = respx.get("https://open-api.bingx.com/test").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": 1})
    )
    ordinary = asyncio.create_task(provider._cached("/test", 300))
    await entered.wait()
    try:
        async with provider.oi_collection_priority():
            urgent = asyncio.create_task(provider._cached("/test", 300))
            release.set()
            assert (await asyncio.wait_for(urgent, 1))[0] == 1
        await ordinary
        assert route.call_count == 1
        assert provider._normal_requests.is_set()
    finally:
        await provider.aclose()


async def test_persistence_failure_is_reported_and_future_boundaries_survive(
    sqlite_database_url, monkeypatch
):
    service = collector(sqlite_database_url)

    def fail(*args):
        raise RuntimeError("DB failure")

    monkeypatch.setattr(service, "_persist", fail)
    report = await service.collect(B)
    assert report["status"] == "FAILED" and report["oi_collection_failed"] == 66
    assert report["oi_collection_successful"] == 0
    assert not service.runtime.oi_lock.locked()
    assert service.runtime.oi_task is None


# --- 24/7 runner loop -------------------------------------------------------------


def _clock(*moments):
    values = iter(moments)
    return lambda: next(values)


async def test_runner_loop_restart_at_10_07_targets_10_15_then_10_30():
    from app.scanner.runner import collect_oi_forever

    stop, targets = asyncio.Event(), []
    t = datetime(2026, 9, 24, 10, 7, 13, tzinfo=UTC)
    clock = _clock(
        t,  # restart at 10:07 -> target 10:15 (10:00 is never fabricated)
        t.replace(minute=15, second=0) + timedelta(milliseconds=200),
        t.replace(minute=15, second=40),  # after collecting -> next target 10:30
        t.replace(minute=30, second=0) + timedelta(milliseconds=100),
    )

    async def collect(target):
        targets.append(target)
        if len(targets) == 2:
            stop.set()
        return {}

    await collect_oi_forever(stop, collect, clock)
    assert targets == [
        datetime(2026, 9, 24, 10, 15, tzinfo=UTC),
        datetime(2026, 9, 24, 10, 30, tzinfo=UTC),
    ]


async def test_runner_loop_skips_missed_boundaries_and_survives_errors():
    from app.scanner.runner import collect_oi_forever

    stop, targets = asyncio.Event(), []
    t = datetime(2026, 9, 24, 10, 14, 59, tzinfo=UTC)
    clock = _clock(
        t,
        t + timedelta(seconds=2),
        # Collection hung (or the host slept) until 10:47: next target is 11:00,
        # never a late 10:30 or 10:45.
        datetime(2026, 9, 24, 10, 47, tzinfo=UTC),
        datetime(2026, 9, 24, 11, 0, 1, tzinfo=UTC),
    )

    async def collect(target):
        targets.append(target)
        if len(targets) == 1:
            raise RuntimeError("provider down")
        stop.set()
        return {}

    await collect_oi_forever(stop, collect, clock)
    assert [t.strftime("%H:%M") for t in targets] == ["10:15", "11:00"]


async def test_runner_loop_stops_promptly_while_waiting():
    from app.scanner.runner import collect_oi_forever

    stop = asyncio.Event()
    collect = AsyncMock()
    task = asyncio.create_task(collect_oi_forever(stop, collect))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    collect.assert_not_called()


def test_mark_feed_parses_captured_live_frame_verbatim():
    """Frame captured from wss://open-api-swap.bingx.com/swap-market on 2026-09-24."""
    from app.providers.bingx_mark_prices import parse_mark

    frame = (
        '{"code":0,"dataType":"BTC-USDT@markPrice","data":{"e":"markPriceUpdate",'
        '"E":1790245855754,"s":"BTC-USDT","p":"83177.8"}}'
    )
    event = datetime.fromtimestamp(1790245855.754, UTC)
    assert parse_mark(frame, event - timedelta(seconds=1), event) == (
        "BTC-USDT",
        Decimal("83177.8"),
    )
    ack = '{"id":"BTC-USDT","code":0,"msg":"","dataType":"","data":null}'
    assert parse_mark(ack, event, event) is None


async def test_runner_loop_never_collects_before_the_boundary_after_early_wakeup():
    """Regression: the event-loop timer woke 6-42 ms before :15/:45 live, and the
    collector rejected the target (ValueError), losing the boundary."""
    from app.scanner.runner import collect_oi_forever

    stop, targets = asyncio.Event(), []
    target = datetime(2026, 9, 24, 19, 15, tzinfo=UTC)
    clock = _clock(
        target - timedelta(minutes=2),  # choose target
        target - timedelta(minutes=2),  # initial delay
        target - timedelta(milliseconds=6),  # timer fired early
        target + timedelta(milliseconds=1),  # re-check: boundary reached
        target + timedelta(milliseconds=2),  # passed to collect (recorded below)
    )

    async def collect(boundary):
        targets.append(boundary)
        stop.set()
        return {}

    import app.scanner.runner as runner_module

    original = runner_module.asyncio.wait_for

    async def instant(awaitable, timeout):  # simulate timers firing immediately
        awaitable.close()
        raise TimeoutError

    runner_module.asyncio.wait_for = instant
    try:
        await collect_oi_forever(stop, collect, clock)
    finally:
        runner_module.asyncio.wait_for = original
    assert targets == [target]


@pytest.mark.parametrize(
    "ahead,accepted",
    [
        (0.08, True),  # measured live: exchange clock ~80 ms ahead of this host
        (0.5, True),
        (2.0, True),  # boundary: exactly the tolerance is accepted (inclusive)
        (2.001, False),  # anything beyond the tolerance is rejected
        (30.0, False),  # clearly in the future
    ],
)
async def test_small_future_clock_skew_is_tolerated_only_up_to_the_limit(
    sqlite_database_url, ahead, accepted
):
    now = B + timedelta(seconds=10)
    result = await collector(
        sqlite_database_url,
        SamplingExchange(count=3, offset=10 + ahead),
        clock=lambda: now,
    ).collect(B)
    assert result["oi_collection_successful"] == (3 if accepted else 0)
    assert result["oi_observations_unaligned"] == (0 if accepted else 3)
    if accepted:  # bucket alignment unchanged: stored at the boundary itself
        with get_session_factory(sqlite_database_url)() as session:
            buckets = set(session.scalars(select(OpenInterestSnapshot.bucket_at)))
        assert {b.replace(tzinfo=UTC) for b in buckets} == {B}


async def test_skew_tolerance_never_admits_stale_or_preboundary_observations(
    sqlite_database_url,
):
    # Stale: taken beyond the boundary tolerance (150 s) is still rejected.
    stale = await collector(
        sqlite_database_url,
        SamplingExchange(count=3, offset=151),
        clock=lambda: B + timedelta(seconds=151),
    ).collect(B)
    assert stale["oi_collection_successful"] == 0
    # Observed before the boundary by more than the tolerance: still unaligned.
    early = await collector(
        sqlite_database_url, SamplingExchange(count=3, offset=-151)
    ).collect(B)
    assert early["oi_observations_unaligned"] == 3
    assert early["oi_collection_successful"] == 0
