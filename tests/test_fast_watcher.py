"""FastMarketWatcher: adaptive detection, anti-spam, dispatch, rescan, transport."""

import asyncio
import gzip
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from sqlalchemy import select

from app.config import Settings
from app.db.scanner_models import FastMarketEvent, MarketSetup, ScannerSnapshot
from app.db.session import get_session_factory
from app.fast.detector import AntiSpam, FastConfig, FastState, SymbolTracker, evaluate
from app.fast.stream import StreamConnection
from app.fast.watcher import FastJob, FastMarketWatcher
from app.scanner.domain import Contract, Ticker
from app.services.scanner_service import ScannerRuntime, ScannerService
from app.telegram.fast import tradingview_url
from tests.scanner_fixtures import FixtureFutures
from tests.test_scanner_service import save_ready

T0 = 1_790_000_000.0
CONFIG = FastConfig()


def tracker(sigma_pct=0.1, minutes=60, price=100.0, volume=1000.0):
    """Baseline of closed 1m candles alternating by `sigma_pct`."""
    t = SymbolTracker("AAAUSDT")
    closes, p = [], price
    first = int(T0 // 60 * 60 - minutes * 60) * 1000
    for i in range(minutes):
        p = p * (1 + (sigma_pct / 100) * (1 if i % 2 else -1))
        closes.append((first + i * 60_000, p, volume))
    t.seed(closes)
    return t


def feed(t, prices, start=T0, step=1.0, volume_per_tick=10.0, minute_open=None):
    """Realtime kline updates, one per `step` seconds, within one minute."""
    open_ms = int(minute_open or start // 60 * 60) * 1000
    total = 0.0
    for i, price in enumerate(prices):
        total += volume_per_tick
        t.on_kline(open_ms, price, total, start + i * step)
    return start + (len(prices) - 1) * step


# --- detection ------------------------------------------------------------------


def test_sudden_one_minute_move_triggers_fast_alert():
    t = tracker(sigma_pct=0.1)
    now = feed(t, [100.0] * 30 + [100 + 0.12 * i for i in range(1, 31)])  # +3.6% / 30 s
    trigger = evaluate(t, now, CONFIG)
    assert trigger is not None and trigger.direction == "LONG"
    assert trigger.state in (FastState.FAST_MOVE, FastState.EXTREME_MOVE)
    assert trigger.change_pct > 3 and trigger.reasons[0].startswith(
        "PRICE_ACCELERATION"
    )


def test_ordinary_volatility_does_not_trigger():
    t = tracker(sigma_pct=0.1)
    wiggle = [100 + 0.05 * (1 if i % 2 else -1) for i in range(60)]  # +-0.05%
    assert evaluate(t, feed(t, wiggle), CONFIG) is None


def test_threshold_adapts_to_each_assets_volatility():
    move = [100.0] * 30 + [100 + 0.07 * i for i in range(1, 31)]  # about +2.1%
    calm, wild = tracker(sigma_pct=0.1), tracker(sigma_pct=1.0)
    assert evaluate(calm, feed(calm, move), CONFIG) is not None
    assert evaluate(wild, feed(wild, move), CONFIG) is None  # normal for a wild asset
    assert wild.sigma_1m_pct() > 5 * calm.sigma_1m_pct()


def test_unarmed_or_thin_streams_are_filtered():
    move = [100.0] * 30 + [100 + 0.2 * i for i in range(1, 31)]
    short_history = tracker(minutes=5)
    assert evaluate(short_history, feed(short_history, move), CONFIG) is None
    thin = tracker()
    now = feed(thin, [100.0, 100.0, 106.0], step=20)  # 3 updates a minute
    assert evaluate(thin, now, CONFIG) is None


def test_volume_expansion_confirms_momentum():
    t = tracker(sigma_pct=0.1, volume=1000)
    now = feed(
        t, [100.0] * 30 + [100 + 0.05 * i for i in range(1, 31)], volume_per_tick=150
    )
    trigger = evaluate(t, now, CONFIG)
    assert trigger.state == FastState.CONFIRMED_MOMENTUM
    assert "VOLUME_EXPANSION" in trigger.reasons and trigger.volume_ratio > 3


def test_extreme_relative_to_baseline():
    t = tracker(sigma_pct=0.05)
    now = feed(t, [100.0] * 30 + [100 + 0.3 * i for i in range(1, 31)])  # +9%
    assert evaluate(t, now, CONFIG).state == FastState.EXTREME_MOVE


def test_minute_rollover_adds_only_closed_minutes_to_baseline():
    t = tracker(minutes=20)
    feed(t, [100.0] * 5, start=T0 - 70, minute_open=(T0 - 70) // 60 * 60)
    before = len(t.closed)
    feed(t, [101.0], start=T0 + 60, minute_open=(T0 + 60) // 60 * 60)
    assert len(t.closed) == before + 1  # previous minute closed exactly once
    assert t.closed[-1] == (100.0, 50.0)


# --- anti-spam ------------------------------------------------------------------


def trig(state=FastState.FAST_MOVE, change=3.0, reasons=("PRICE_ACCELERATION_1M",)):
    from app.fast.detector import Trigger

    return Trigger("LONG", state, "1m", change, 1.0, 0.2, None, list(reasons), 103.0)


def test_duplicates_suppressed_and_upgrades_sent_once():
    spam = AntiSpam(CONFIG)
    assert spam.decide("AAAUSDT", trig(), T0) == "NEW"
    assert spam.decide("AAAUSDT", trig(), T0 + 5) is None
    confirmed = trig(
        FastState.CONFIRMED_MOMENTUM, 3.2, ("PRICE_ACCELERATION_1M", "VOLUME_EXPANSION")
    )
    assert spam.decide("AAAUSDT", confirmed, T0 + 10) == "UPGRADE"
    assert spam.decide("AAAUSDT", confirmed, T0 + 15) is None
    assert (
        spam.decide("AAAUSDT", trig(FastState.EXTREME_MOVE, 3.3), T0 + 20) == "UPGRADE"
    )
    assert spam.decide("AAAUSDT", trig(FastState.EXTREME_MOVE, 3.4), T0 + 25) is None
    assert (
        spam.decide("AAAUSDT", trig(FastState.EXTREME_MOVE, 5.1), T0 + 30)
        == "EXTENSION"
    )
    # Other direction and other symbols are independent episodes.
    down = trig(change=-3.0)
    down.direction = "SHORT"
    assert spam.decide("AAAUSDT", down, T0 + 31) == "NEW"
    # A genuinely new event after the independent fast cooldown.
    assert (
        spam.decide("AAAUSDT", trig(), T0 + 30 + CONFIG.cooldown_seconds + 1) == "NEW"
    )


# --- watcher: dispatch, context, rescan -------------------------------------------


class FakeBingX:
    def __init__(self, oi=None):
        self.oi = oi

    async def open_interest_notional(self, symbol):
        if self.oi is None:
            raise RuntimeError("OI endpoint down")
        return self.oi

    async def contracts(self):
        return [
            Contract(
                symbol=s,
                base_asset=s[:-4],
                quote_asset="USDT",
                contract_type="PERPETUAL",
                status="TRADING",
            )
            for s in ("AAAUSDT", "BIGUSDT", "THINUSDT", "PORTUSDT")
        ]

    async def tickers(self):
        now = datetime.now(UTC)
        volumes = {
            "AAAUSDT": 50e6,
            "BIGUSDT": 900e6,
            "THINUSDT": 200e3,
            "PORTUSDT": 2e6,
        }
        return {
            s: Ticker(symbol=s, price=1, quote_volume=v, timestamp=now)
            for s, v in volumes.items()
        }


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def __call__(self, text, reply_to, buttons):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((text, reply_to, buttons))
        return [500 + len(self.sent)]


def watcher_for(url, sender=None, oi=None, rescan=None, **settings):
    return FastMarketWatcher(
        Settings(**{"fast_send_telegram": True, "early_send_telegram": True, **settings}), get_session_factory(url), FakeBingX(oi), sender, rescan
    )


async def test_alert_with_technical_score_below_80_is_labelled_early_warning(
    session, sqlite_database_url
):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    # A plain fast move is research-only; one coinciding with a fresh structural
    # break is told (FAST_MOVE policy).
    await watcher.dispatch(
        FastJob(
            "AAAUSDT",
            trig(change=3.4),
            "NEW",
            time.time() - 0.01,
            time.time(),
            extra_reasons=["BREAKOUT_ACCELERATION"],
        )
    )
    text, _, buttons = sender.sent[0]
    assert text.startswith("⚡ <b>РЕЗКОЕ ДВИЖЕНИЕ</b>")
    assert "🟢 <b>+3.40%</b> за 1 мин" in text
    assert "⚠️ Это раннее предупреждение." in text
    assert "Полный HIGH_CONFLUENCE setup ещё не подтверждён." in text
    assert "📊 OI: нет данных" in text and "⭐ Technical score: нет данных" in text
    assert buttons == [
        ("📈 TradingView", "https://www.tradingview.com/chart/?symbol=BINGX:AAAUSDT.P")
    ]
    for banned in ("buy", "sell", "enter now", "guaranteed"):
        assert banned not in text.lower()
    row = session.scalar(select(FastMarketEvent))
    assert (
        row.sent and row.telegram_message_id == 501 and row.latency_ms["total_ms"] >= 0
    )


async def test_active_setup_move_keeps_score_and_threads_reply(
    session, sqlite_database_url
):
    _, _, _, setup = save_ready(session)
    setup.notified_data = {"telegram_message_id": 4242}
    session.commit()
    score = setup.score
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender, oi=None)
    await watcher.dispatch(
        FastJob("AAAUSDT", trig(change=2.8), "NEW", time.time(), time.time())
    )
    text, reply_to, _ = sender.sent[0]
    assert text.startswith("⚡ <b>ДВИЖЕНИЕ ПО АКТИВНОМУ СЕТАПУ</b>")
    assert f"LONG {score}/100" in text and "оценка не меняется" in text
    assert reply_to == 4242
    session.expire_all()
    assert session.get(MarketSetup, setup.id).score == score  # never modified


async def test_oi_and_news_failures_never_block_the_alert(
    session, sqlite_database_url, monkeypatch
):
    monkeypatch.setattr(
        "app.news.repository.setup_news_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("news down")),
    )
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender, oi=None)
    confirmed = trig(FastState.CONFIRMED_MOMENTUM)
    await watcher.dispatch(
        FastJob("AAAUSDT", confirmed, "NEW", time.time(), time.time())
    )
    assert len(sender.sent) == 1 and "📊 OI: нет данных" in sender.sent[0][0]


async def test_telegram_failure_is_recorded_not_retried(session, sqlite_database_url):
    watcher = watcher_for(sqlite_database_url, Recorder(fail=True))
    await watcher.dispatch(
        FastJob(
            "AAAUSDT",
            trig(FastState.EXTREME_MOVE),
            "NEW",
            time.time(),
            time.time(),
        )
    )
    row = session.scalar(select(FastMarketEvent))
    assert not row.sent and row.error == "RuntimeError"


async def test_stream_message_path_detects_queues_and_requests_one_rescan(
    sqlite_database_url,
):
    rescans = []
    watcher = watcher_for(sqlite_database_url, rescan=AsyncMock())
    watcher.request_rescan = lambda symbol: rescans.append(symbol)  # observe requests
    watcher.trackers["AAAUSDT"] = tracker()
    start = time.time() - 59
    open_ms = int(start // 60 * 60) * 1000
    current = {"t": start}
    # Replayed ticks carry past receipt times; the watcher clock follows them so the
    # metric measures processing (receipt -> detection), not replay age.
    watcher.clock = lambda: current["t"] + 0.0005
    for i, price in enumerate([100.0] * 30 + [100 + 0.12 * i for i in range(1, 31)]):
        current["t"] = start + i
        watcher.on_kline("AAA-USDT", open_ms, price, 10.0 * (i + 1), start + i)
    assert watcher.outbox.qsize() >= 1 and set(rescans) == {"AAAUSDT"}
    assert watcher.counters["fast_events_suppressed"] > 0  # later ticks deduplicated
    assert watcher.latency["fast_detection_ms"].percentile(95) < 3000


async def test_rescan_requests_are_one_symbol_and_deduplicated(sqlite_database_url):
    watcher = watcher_for(sqlite_database_url, rescan=AsyncMock())
    watcher.request_rescan("AAAUSDT")
    watcher.request_rescan("AAAUSDT")
    assert watcher.rescans.qsize() == 1


async def test_priority_rescan_is_one_symbol_closed_candles_and_scores_intact(session):
    runtime = ScannerRuntime(FixtureFutures(), Settings())
    now = datetime(2026, 9, 24, 12, 7, 30, tzinfo=UTC)
    run = await ScannerService(session, runtime).run(now, only="AAAUSDT")
    snapshots = session.scalars(select(ScannerSnapshot)).all()
    assert run.universe_size == 1 and [s.symbol for s in snapshots] == ["AAAUSDT"]
    closed = snapshots[0].candle_closed_at.replace(tzinfo=UTC)
    assert closed < datetime(
        2026, 9, 24, 12, 0, tzinfo=UTC
    )  # 12:00-12:15 not closed yet
    # Same inputs through the full scan give the same AAA score: no fast-path bias.
    full = ScannerRuntime(FixtureFutures(), Settings())
    session.execute(ScannerSnapshot.__table__.delete())
    session.commit()
    await ScannerService(session, full).run(now)
    again = session.scalar(
        select(ScannerSnapshot).where(ScannerSnapshot.symbol == "AAAUSDT")
    )
    assert (again.long_score, again.short_score) == (
        snapshots[0].long_score,
        snapshots[0].short_score,
    )


async def test_universe_is_liquidity_filtered_with_priority_symbols_first(
    session, sqlite_database_url
):
    _, _, _, setup = save_ready(session)
    setup.symbol = "PORTUSDT"  # active setup on a less liquid but tradable market
    session.commit()
    watcher = watcher_for(sqlite_database_url)
    universe = await watcher.universe()
    assert universe == ["PORTUSDT", "BIGUSDT", "AAAUSDT"]  # THIN excluded
    assert "THINUSDT" not in universe


async def test_backpressure_keeps_extreme_events(sqlite_database_url):
    watcher = watcher_for(sqlite_database_url, fast_queue_size=10)
    for index in range(10):
        watcher.outbox.put_nowait(1, f"move-{index}")
    assert watcher.outbox.put_nowait(0, "extreme")  # evicts a lower-priority move
    assert not watcher.outbox.put_nowait(1, "another")
    assert (await watcher.outbox.get())[1] == "extreme"


async def test_cooldown_survives_restart(session, sqlite_database_url):
    first = watcher_for(sqlite_database_url, Recorder())
    first.antispam.decide("AAAUSDT", trig(), time.time())
    await first.dispatch(FastJob("AAAUSDT", trig(), "NEW", time.time(), time.time()))
    restarted = watcher_for(sqlite_database_url, Recorder())
    restarted.restore_cooldowns()
    assert restarted.antispam.decide("AAAUSDT", trig(), time.time()) is None


async def test_watcher_start_failure_does_not_stop_the_scanner():
    from app.scanner.runner import maybe_start_fast_watcher

    async def broken(settings):
        raise ConnectionError("bingx unreachable")

    assert await maybe_start_fast_watcher(Settings(), broken) is None


def test_tradingview_link_format():
    assert (
        tradingview_url("ARBUSDT")
        == "https://www.tradingview.com/chart/?symbol=BINGX:ARBUSDT.P"
    )


# --- transport ------------------------------------------------------------------


class FakeSocket:
    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def send_json(self, data):
        self.sent.append(data)

    async def send_str(self, data):
        self.sent.append(data)

    async def receive(self):
        if not self.frames:
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
        return self.frames.pop(0)

    async def close(self):
        pass


def frame(payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        type=aiohttp.WSMsgType.BINARY, data=gzip.compress(raw.encode())
    )


def kline(price):
    return frame(
        {
            "code": 0,
            "dataType": "AAA-USDT@kline_1m",
            "s": "AAA-USDT",
            "data": [
                {
                    "c": str(price),
                    "o": "1",
                    "h": "2",
                    "l": "1",
                    "v": "5",
                    "T": 1790271300000,
                }
            ],
        }
    )


async def test_stream_resubscribes_after_drop_and_answers_ping():
    sockets = [FakeSocket(["Ping", kline(1.0)]), FakeSocket([kline(2.0)])]
    sockets[0].frames[0] = frame("Ping")
    opened = []

    async def connect():
        opened.append(sockets[len(opened)] if len(opened) < 2 else FakeSocket([]))
        return opened[-1]

    seen = []
    stream = StreamConnection(
        0,
        ["AAA-USDT", "BBB-USDT"],
        lambda *a: seen.append(a[2]),
        max_backoff=0.01,
        connect=connect,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    deadline = time.monotonic() + 3
    while len(seen) < 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert seen[:2] == [1.0, 2.0] and stream.reconnects >= 1
    subs = [m for m in opened[0].sent if isinstance(m, dict)]
    assert [m["dataType"] for m in subs] == ["AAA-USDT@kline_1m", "BBB-USDT@kline_1m"]
    assert "Pong" in opened[0].sent  # heartbeat answered
    assert [m["dataType"] for m in opened[1].sent if isinstance(m, dict)] == [
        "AAA-USDT@kline_1m",
        "BBB-USDT@kline_1m",
    ]  # full resubscription after reconnect


def test_seed_after_stream_rollover_prepends_history_instead_of_skipping():
    """Regression: 74/84 symbols were never seeded because the stream had already
    closed a minute before the REST baseline arrived."""
    t = SymbolTracker("AAAUSDT")
    minute = int(T0 // 60 * 60) * 1000
    t.on_kline(minute, 100.0, 10.0, T0)
    t.on_kline(minute + 60_000, 101.0, 5.0, T0 + 61)  # stream closed minute 1
    history = [(minute - (60 - i) * 60_000, 99.0 + i / 100, 7.0) for i in range(60)]
    history.append((minute, 555.0, 555.0))  # overlaps the streamed minute: ignored
    t.seed(history)
    assert len(t.closed) == 60  # capped baseline, now armed
    assert t.closed[-1] == (100.0, 10.0)  # the streamed closed minute is kept, last
    assert (555.0, 555.0) not in t.closed


def test_sub_minute_flick_without_volume_is_noise_but_counts_with_volume():
    flick = [100.0] * 50 + [100.0 - 0.07 * i for i in range(1, 11)]  # -0.7% in 10 s
    quiet = tracker(sigma_pct=0.05)
    assert evaluate(quiet, feed(quiet, flick), CONFIG) is None
    busy = tracker(sigma_pct=0.05, volume=100)
    flick_now = feed(
        busy,
        [100.0] * 50 + [100.0 - 0.12 * i for i in range(1, 11)],
        volume_per_tick=20,
    )
    trigger = evaluate(busy, flick_now, CONFIG)
    assert trigger is not None and "VOLUME_EXPANSION" in trigger.reasons
