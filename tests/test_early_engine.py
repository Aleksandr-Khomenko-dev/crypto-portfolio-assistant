"""Early trend ignition / pattern formation / breakout-retest engine.

Covers the early-event state machine, late-move protection, anti-spam, restart
safety, persistence, Telegram output, outcome causality and score isolation. Fast
move detection itself is covered in test_fast_watcher.py.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from sqlalchemy import select

from app.analytics.analysis import analyze_frame
from app.config import Settings
from app.db.scanner_models import (
    FastMarketEvent,
    MarketSetup,
    MarketStructureEpisode,
    PatternCandidateRecord,
    ScannerSnapshot,
)
from app.db.session import get_session_factory
from app.early.context import StructureContext, extension, market_bias
from app.early.engine import EarlyAntiSpam, EarlyEngine
from app.early.model import EarlyConfig, EarlyEvent, EventType, KeyZone
from app.early.outcomes import (
    AMBIGUOUS,
    INVALIDATION_FIRST,
    TARGET_FIRST,
    EventRecord,
    calibration,
    measure,
    successor,
)
from app.early.patterns import detect_patterns
from app.fast.detector import FastState, SymbolTracker, Trigger
from app.fast.stream import StreamConnection
from app.fast.watcher import EarlyJob, FastMarketWatcher, chat_fingerprint
from app.news.pipeline import BoundedPriorityQueue
from app.scanner.domain import Candle, Contract, Pivot, StructureBreak, Ticker
from app.scanner.scoring import signal_state
from app.telegram.early import EarlyContext, format_early_event
from tests.scanner_fixtures import candles
from tests.test_scanner_service import save_ready

T0 = 1_789_999_800.0  # a 5m close
BUILT = datetime.fromtimestamp(T0, UTC)
CFG = EarlyConfig()
SYMBOL = "GASUSDT"


def zone(kind, lower, upper, atr=1.0, tf="1h", touches=3):
    return KeyZone(tf, kind, lower, upper, BUILT - timedelta(hours=6), touches, atr)


RES = zone("RESISTANCE", 104.0, 104.5)
SUP = zone("SUPPORT", 99.0, 99.5)


def bars_from(ohlc, end=T0, step=300):
    result = []
    for k, (o, h, low, c) in enumerate(ohlc):
        start = datetime.fromtimestamp(end - (len(ohlc) - k) * step, UTC)
        result.append(
            Candle(
                open_time=start,
                close_time=start + timedelta(seconds=step, milliseconds=-1),
                open=D(str(o)),
                high=D(str(h)),
                low=D(str(low)),
                close=D(str(c)),
                volume=D(100),
            )
        )
    return result


def path(closes, pad=0.1, first=None):
    ohlc, previous = [], first if first is not None else closes[0]
    for c in closes:
        ohlc.append((previous, max(previous, c) + pad, min(previous, c) - pad, c))
        previous = c
    return ohlc


def context(bars5, zones=(RES,), atr1h=1.0, **extra):
    return StructureContext(
        symbol=extra.pop("symbol", SYMBOL),
        built_at=bars5[-1].close_time,
        atr={"5m": 0.3, "15m": 0.5, "1h": atr1h, "4h": 2 * atr1h},
        zones=list(zones),
        bars5=bars5,
        **extra,
    )


def live_tracker(sigma=0.05, volume=1000.0, price=104.0):
    """1m baseline near the live price (realised volatility `sigma` percent)."""
    t = SymbolTracker(SYMBOL)
    first = int(T0 // 60 * 60 - 3600) * 1000
    p, history = price, []
    for i in range(60):
        p *= 1 + (sigma / 100) * (1 if i % 2 else -1)
        history.append((first + i * 60_000, p, volume))
    t.seed(history)
    return t


class Feed:
    """Realtime ticks, one per second, through the engine (as the watcher does)."""

    def __init__(self, engine, tracker, start=T0 + 10, volume=10.0):
        self.engine, self.tracker, self.at, self.volume = engine, tracker, start, volume
        self.minute, self.total = None, 0.0
        self.events: list[EarlyEvent] = []

    def ticks(self, price, count=1):
        for _ in range(count):
            minute = int(self.at // 60 * 60) * 1000
            if minute != self.minute:
                self.minute, self.total = minute, 0.0
            self.total += self.volume
            self.tracker.on_kline(
                minute, price, self.total, self.at, price, price, price
            )
            self.events += self.engine.on_tick(SYMBOL, self.tracker, self.at)
            self.at += 1
        return self

    def types(self):
        return [e.event_type for e in self.events]


def rising_bars(low=102.4, top=104.2, n=20):
    closes = [low + (top - low) * i / (n - 1) for i in range(n)]
    return bars_from(path(closes))


def engine_with(ctx, **kwargs):
    engine = EarlyEngine(CFG, **kwargs)
    engine.contexts[ctx.symbol] = ctx
    return engine


# --- 1-2. breakout approach -----------------------------------------------------------


def test_approach_to_1h_resistance_is_breakout_approach():
    engine = engine_with(context(rising_bars(101.9, 103.2)))
    feed = Feed(engine, live_tracker(), volume=30).ticks(103.0, 70).ticks(103.8, 30)
    approach = [e for e in feed.events if e.event_type == EventType.BREAKOUT_APPROACH]
    assert len(approach) == 1  # once, not every tick
    event = approach[0]
    assert event.direction == "LONG" and event.zone == RES
    assert event.metrics["distance_to_level_atr"] == 0.2
    assert {"REPEATED_TESTS", "VOLUME_RISING"} <= set(event.reasons)
    # The zone itself is recorded as context; the notification policy never sends it.
    assert feed.types().count(EventType.ZONE_WATCH) <= 1


def test_restart_near_a_level_is_not_an_approach():
    """Regression (live 2026-09-24): 28 approaches fired right after a restart for
    markets that were ALREADY near a level; arrival must be observed live."""
    engine = engine_with(context(rising_bars(101.9, 103.2)))
    feed = Feed(engine, live_tracker(), volume=30).ticks(103.8, 180)
    assert EventType.BREAKOUT_APPROACH not in feed.types()
    assert EventType.ZONE_WATCH not in feed.types()


def test_static_level_evidence_alone_is_not_an_approach():
    engine = engine_with(context(rising_bars(101.9, 103.2)))
    feed = Feed(engine, live_tracker(), volume=10).ticks(103.0, 70).ticks(103.8, 30)
    assert EventType.BREAKOUT_APPROACH not in feed.types()  # only REPEATED_TESTS


def test_several_zones_give_one_approach_per_symbol_and_direction():
    near = zone("RESISTANCE", 104.05, 104.2, tf="15m")
    far_4h = zone("RESISTANCE", 104.6, 104.9, tf="4h")
    engine = engine_with(context(rising_bars(101.9, 103.2), zones=(RES, near, far_4h)))
    feed = Feed(engine, live_tracker(), volume=30).ticks(102.8, 70).ticks(103.9, 30)
    assert feed.types().count(EventType.BREAKOUT_APPROACH) == 1


def test_price_far_from_level_sends_no_approach():
    engine = engine_with(context(rising_bars(101.9, 102.2)))
    feed = Feed(engine, live_tracker()).ticks(102.0, 120)
    assert feed.events == []


# --- 3-4. ignition --------------------------------------------------------------------

IGNITION = [
    101.5,
    101.0,
    100.6,
    100.2,
    99.9,
    99.6,
    99.3,
    99.1,
    99.3,
    99.6,
    99.8,
    99.9,
]


def ignition_bars(prefix=()):
    ohlc = list(prefix) + path(IGNITION, pad=0.15)
    sweep = len(prefix) + 7
    o, h, _, c = ohlc[sweep]
    ohlc[sweep] = (o, h, 98.8, c)  # liquidity sweep below the 1H support
    return bars_from(ohlc)


def ignition_context(bars, direction="LONG", zones=(SUP,), **extra):
    kind = "HIGH" if direction == "SHORT" else "LOW"
    ctx = context(
        bars,
        zones=zones,
        breaks5=[
            StructureBreak(
                index=0,
                timestamp=bars[-2].close_time,
                level=bars[-3].high if direction == "LONG" else bars[-3].low,
                direction=direction,
                kind="CHoCH",
            )
        ],
        pivots5=[
            Pivot(
                index=0,
                confirmed_index=0,
                price=bars[-4].low if kind == "LOW" else bars[-4].high,
                kind=kind,
                label="HL" if kind == "LOW" else "LH",
                timestamp=bars[-4].close_time,
                confirmed_at=bars[-2].close_time,
            )
        ],
        rvol5=2.0,
        **extra,
    )
    return ctx


def mirror(bars):
    return [
        b.model_copy(
            update={
                "open": 200 - b.open,
                "high": 200 - b.low,
                "low": 200 - b.high,
                "close": 200 - b.close,
            }
        )
        for b in bars
    ]


def test_bullish_reaction_at_1h_support_with_5m_shift_is_bullish_ignition():
    ctx = ignition_context(ignition_bars())
    engine = EarlyEngine(CFG)
    events = engine.set_context(ctx, 99.9, T0 + 5)
    assert [e.event_type for e in events] == [EventType.BULLISH_IGNITION]
    event = events[0]
    assert event.direction == "LONG" and event.zone == SUP
    assert {"LIQUIDITY_SWEEP", "ZONE_RECLAIM", "CHOCH_5M", "HIGHER_LOW"} <= set(
        event.reasons
    )
    assert 55 <= event.strength <= 100
    assert event.metrics["distance_from_origin_atr"] <= CFG.late_origin_atr


def test_bearish_mirror_is_bearish_ignition():
    bars = mirror(ignition_bars())
    resistance = zone("RESISTANCE", 100.5, 101.0)
    ctx = ignition_context(bars, "SHORT", zones=(resistance,))
    events = EarlyEngine(CFG).set_context(ctx, 100.1, T0 + 5)
    assert [e.event_type for e in events] == [EventType.BEARISH_IGNITION]
    assert events[0].direction == "SHORT" and "LOWER_HIGH" in events[0].reasons


def test_no_ignition_without_a_structural_shift():
    bars = ignition_bars()
    ctx = context(bars, zones=(SUP,), rvol5=3.0)  # volume alone is not a shift
    ctx_events = EarlyEngine(CFG).set_context(ctx, 99.9, T0 + 5)
    assert EventType.BULLISH_IGNITION not in [e.event_type for e in ctx_events]


# --- 5. formation ---------------------------------------------------------------------


def triangle_bars():
    """Flat 105 top touched three times, rising lows 102.5 / 103.3 / 104.0; swing
    bars carry wicks (a bar closing exactly at its high is never a swing high)."""
    legs = [101.0, 105.0, 102.5, 105.0, 103.3, 105.0, 104.0, 104.7]
    closes, swings = [], set()
    for a, b in pairwise(legs):
        closes += [a + (b - a) * k / 4 for k in range(4)]
        swings.add(len(closes))
    closes.append(legs[-1])
    ohlc = path(closes, pad=0.05)
    for i in swings - {len(closes) - 1}:
        o, h, low, c = ohlc[i]
        ohlc[i] = (o, h + 0.1, low, c) if c >= 105 else (o, h, low - 0.1, c)
    return bars_from(ohlc)


def test_ascending_triangle_candidate_before_completion():
    bars = triangle_bars()
    found = [
        p
        for p in detect_patterns(bars, "15m", 1.0, [], CFG)
        if p.pattern_type == "ASCENDING_TRIANGLE"
    ]
    assert len(found) == 1
    pattern = found[0]
    assert pattern.direction == "LONG" and pattern.touch_count >= 2
    assert float(bars[-1].close) < pattern.boundary_level  # not broken yet
    assert 0 <= pattern.distance_to_trigger_atr <= CFG.pattern_level_atr
    ctx = context(bars, zones=())
    ctx.patterns = found
    events = EarlyEngine(CFG).set_context(ctx, float(bars[-1].close), T0 + 5)
    formation = [e for e in events if e.event_type == EventType.FORMATION_WATCH]
    assert formation and formation[0].pattern is pattern
    assert formation[0].strength == pattern.formation_strength


# --- 6-10. first break, retest, failure -----------------------------------------------


def break_engine(bars=None, **ctx_extra):
    ctx = context(bars or rising_bars(), **ctx_extra)
    return engine_with(ctx), live_tracker()


def test_sustained_intrabar_penetration_is_first_break():
    engine, tracker = break_engine()
    feed = Feed(engine, tracker).ticks(104.3, 70).ticks(104.8, 20)
    breaks = [e for e in feed.events if e.event_type == EventType.FIRST_BREAK]
    assert len(breaks) == 1  # duplicate FIRST_BREAK suppressed by the episode
    event = breaks[0]
    assert event.direction == "LONG" and event.episode.phase == "BROKEN"
    assert event.metrics["penetration_atr"] >= CFG.break_atr
    assert event.metrics["hold_seconds"] >= CFG.break_hold_seconds


def test_single_noisy_tick_is_not_a_break():
    engine, tracker = break_engine()
    feed = Feed(engine, tracker).ticks(104.3, 70).ticks(105.5, 1).ticks(104.3, 30)
    assert EventType.FIRST_BREAK not in feed.types()
    assert not engine.episodes[SYMBOL]


def test_break_needs_the_transition_observed_live():
    """Restart safety: price already above the level at start is not a fresh break."""
    engine, tracker = break_engine()
    feed = Feed(engine, tracker).ticks(104.9, 120)
    assert EventType.FIRST_BREAK not in feed.types()


def test_return_to_broken_zone_is_retest_watch_then_confirmed_once():
    engine, tracker = break_engine(
        oi_change_15m=0.4, market={"BTC": "neutral", "ETH": "neutral"}
    )
    feed = (
        Feed(engine, tracker, volume=30)
        .ticks(104.3, 70)
        .ticks(104.8, 15)
        .ticks(105.0, 10)
    )
    feed.ticks(104.6, 3).ticks(104.55, 5)
    assert feed.types().count(EventType.RETEST_WATCH) == 1
    feed.ticks(104.85, 5).ticks(104.9, 30)
    confirmed = [e for e in feed.events if e.event_type == EventType.RETEST_CONFIRMED]
    assert len(confirmed) == 1  # upgrade sends exactly once
    event = confirmed[0]
    assert event.confirmations[0].startswith("нет глубокого возврата")
    assert {"HIGHER_LOW", "VOLUME_RENEWED", "OI_HOLDING"} <= set(event.reasons)
    assert event.metrics["invalidation"] < 104.55  # below the retest extreme
    assert event.episode.phase == "CONFIRMED"


def test_retest_without_enough_confirmation_is_not_confirmed():
    engine, tracker = break_engine()  # no OI, no market context
    base = [b.model_copy(update={"low": D("104.7")}) for b in rising_bars()]
    engine.contexts[SYMBOL] = context(base)  # no higher low below the retest either
    feed = Feed(engine, tracker).ticks(104.3, 70).ticks(104.8, 15).ticks(105.0, 10)
    feed.ticks(104.6, 5).ticks(104.9, 20)
    assert EventType.RETEST_WATCH in feed.types()
    assert EventType.RETEST_CONFIRMED not in feed.types()


def test_deep_return_into_range_is_failed_breakout_sent_once():
    engine, tracker = break_engine()
    feed = Feed(engine, tracker).ticks(104.3, 70).ticks(104.8, 15)
    feed.ticks(103.9, 15).ticks(103.5, 60)
    assert feed.types().count(EventType.FAILED_BREAKOUT) == 1
    failed = next(e for e in feed.events if e.event_type == EventType.FAILED_BREAKOUT)
    assert failed.episode.phase == "FAILED"


# --- 28-29. late-move protection ------------------------------------------------------


def test_heavily_extended_break_becomes_late_extended_move():
    bars = bars_from(path([90.0] + [104.0] * 19))  # origin 10+ ATR below
    engine, tracker = break_engine(bars)
    feed = Feed(engine, tracker).ticks(104.3, 70).ticks(104.8, 20)
    assert EventType.FIRST_BREAK not in feed.types()
    late = [e for e in feed.events if e.event_type == EventType.LATE_EXTENDED_MOVE]
    assert len(late) == 1 and late[0].metrics["distance_from_origin_atr"] > 3
    assert late[0].reasons[0] == "LATE_AFTER_FIRST_BREAK"


def test_plus_15_percent_move_is_not_labelled_fresh_ignition():
    rally_low = path([86.8, 90.0, 95.0, 100.0, 104.0, 103.0])
    ctx = ignition_context(ignition_bars(prefix=rally_low))
    ext = extension(ctx, 99.9, "LONG", CFG)
    assert ext.move_pct > 14 and ext.late
    events = EarlyEngine(CFG).set_context(ctx, 99.9, T0 + 5)
    assert EventType.BULLISH_IGNITION not in [e.event_type for e in events]
    assert [e.event_type for e in events] == [EventType.LATE_EXTENDED_MOVE]
    text = format_early_event(events[0], EarlyContext(), BUILT)
    assert "ДВИЖЕНИЕ УЖЕ РАСТЯНУТО" in text and "НЕ новый ранний LONG" in text


def test_fast_move_far_from_origin_is_classified_late():
    engine = engine_with(context(bars_from(path([90.0] + [104.0] * 19))))
    trigger = Trigger("LONG", FastState.FAST_MOVE, "1m", 2.0, 1.0, 0.1, None, [], 105.0)
    event_type, _, metrics = engine.classify_fast(SYMBOL, trigger, T0 + 30)
    assert event_type == EventType.LATE_EXTENDED_MOVE
    assert metrics["original_event"] == "FAST_MOVE"


# --- anti-spam / backpressure ---------------------------------------------------------


def early(event_type=EventType.BREAKOUT_APPROACH, key="z", strength=50, **metrics):
    return EarlyEvent(
        SYMBOL,
        event_type,
        "LONG",
        100.0,
        T0,
        strength=strength,
        key=key,
        metrics=metrics,
    )


def test_antispam_once_per_key_with_single_material_upgrade():
    spam = EarlyAntiSpam(CFG)
    assert spam.decide(early(distance_to_level_atr=0.3), T0) == "NEW"
    assert spam.decide(early(distance_to_level_atr=0.28), T0 + 5) is None
    assert spam.decide(early(distance_to_level_atr=0.12), T0 + 10) is None  # < 5 min
    assert spam.decide(early(distance_to_level_atr=0.12), T0 + 301) == "UPGRADE"
    assert spam.decide(early(distance_to_level_atr=0.05), T0 + 700) is None
    assert spam.decide(early(EventType.FIRST_BREAK, key="ep1"), T0) == "NEW"
    assert spam.decide(early(EventType.FIRST_BREAK, key="ep1"), T0 + 60) is None
    assert spam.decide(early(EventType.FIRST_BREAK, key="ep2"), T0 + 60) == "NEW"


def test_high_priority_event_survives_queue_pressure():
    queue = BoundedPriorityQueue(3)
    for i in range(3):
        assert queue.put_nowait(3, f"formation-{i}")
    assert queue.put_nowait(0, "first-break")  # evicts a formation
    assert not queue.put_nowait(3, "zone")
    assert asyncio.run(queue.get())[1] == "first-break"


# --- 33-34. BTC / ETH self-reference ---------------------------------------------------


def test_btc_and_eth_are_never_compared_against_themselves():
    majors = {"BTCUSDT": {}, "ETHUSDT": {}}
    assert set(market_bias("BTCUSDT", majors)) == {"ETH"}
    assert set(market_bias("ETHUSDT", majors)) == {"BTC"}
    assert set(market_bias("GASUSDT", majors)) == {"BTC", "ETH"}


# --- 18-21. score isolation, causality, thresholds -------------------------------------


def test_unfinished_candle_does_not_alter_closed_candle_analysis():
    now = datetime(2026, 9, 23, 12, 7, 30, tzinfo=UTC)
    closed = candles("15m", now=now, count=240)
    forming_start = closed[-1].close_time + timedelta(milliseconds=1)
    forming = Candle(
        open_time=forming_start,
        close_time=forming_start + timedelta(minutes=15, milliseconds=-1),
        open=closed[-1].close,
        high=closed[-1].close * 2,
        low=closed[-1].close / 2,
        close=closed[-1].close * 2,
        volume=D(99999),
    )
    settings = Settings()
    a = analyze_frame(closed, "15m", now, settings)
    b = analyze_frame([*closed, forming], "15m", now, settings)
    assert a.model_dump() == b.model_dump()


def test_no_realtime_source_is_imported_by_normal_scoring():
    root = Path(__file__).resolve().parents[1] / "app"
    closed_candle_modules = [
        root / "scanner" / "scoring.py",
        root / "scanner" / "risk.py",
        root / "services" / "scanner_service.py",
        *sorted((root / "analytics").glob("*.py")),
    ]
    for module in closed_candle_modules:
        text = module.read_text()
        assert "app.fast" not in text and "app.early" not in text, module.name


def test_high_and_extreme_thresholds_unchanged():
    settings = Settings()
    assert (settings.scanner_high_score, settings.scanner_extreme_score) == (80, 90)
    assert settings.scanner_alert_score == 80
    assert signal_state(79, settings).value == "SETUP_FORMING"
    assert signal_state(80, settings).value == "HIGH_CONFLUENCE"
    assert signal_state(89, settings).value == "HIGH_CONFLUENCE"
    assert signal_state(90, settings).value == "EXTREME_CONFLUENCE"


# --- 43. outcome causality -------------------------------------------------------------


def test_outcome_uses_only_candles_after_the_event():
    detected = BUILT
    before = bars_from([(100, 130, 70, 100)] * 3, end=T0)  # huge range BEFORE the event
    after = bars_from(
        [
            (100, 100.5, 99.8, 100.4),
            (100.4, 101.2, 100.2, 101.0),
            (101, 102.1, 100.9, 102),
        ],
        end=T0 + 900,
    )
    outcome = measure("LONG", 100.0, 99.0, detected, before + after)
    assert outcome.bars_used == 3
    assert outcome.first_1r == TARGET_FIRST and outcome.first_2r == TARGET_FIRST
    assert outcome.mae_pct == 0.2 and outcome.mfe_pct == 2.1


def test_same_bar_target_and_invalidation_is_ambiguous_not_a_win():
    bars = bars_from([(100, 101.5, 98.5, 100)], end=T0 + 300)
    outcome = measure("LONG", 100.0, 99.0, BUILT, bars)
    assert outcome.first_1r == AMBIGUOUS


def test_short_invalidation_first():
    bars = bars_from([(100, 101.2, 99.9, 101.0)], end=T0 + 300)
    assert measure("SHORT", 100.0, 101.0, BUILT, bars).first_1r == INVALIDATION_FIRST


def test_event_chain_uses_only_later_events():
    first = EventRecord("1", SYMBOL, "LONG", "FIRST_BREAK", BUILT, "ep")
    earlier = EventRecord(
        "0", SYMBOL, "LONG", "FAILED_BREAKOUT", BUILT - timedelta(minutes=5), "ep"
    )
    other_episode = EventRecord(
        "2", SYMBOL, "LONG", "FAILED_BREAKOUT", BUILT + timedelta(minutes=3), "x"
    )
    later = EventRecord(
        "3", SYMBOL, "LONG", "RETEST_CONFIRMED", BUILT + timedelta(minutes=20), "ep"
    )
    assert successor(first, [earlier, other_episode, later], 240) == (
        "RETEST_CONFIRMED",
        20.0,
    )
    assert successor(first, [earlier], 240) == (None, None)


def test_calibration_withholds_rates_below_min_samples():
    rows = [
        {
            "event_type": "RETEST_CONFIRMED",
            "first_1r": TARGET_FIRST,
            "first_2r": INVALIDATION_FIRST,
            "mfe_pct": 2.0,
            "mae_pct": 0.5,
            "continuation": True,
            "next_event_type": None,
        }
    ] * 5
    report = calibration(rows, min_samples=30)["RETEST_CONFIRMED"]
    assert report["sample"] == 5 and report["warning"].startswith("LOW_SAMPLE")
    assert report["plus_1r_before_invalidation"] is None
    enough = calibration(rows * 6, min_samples=30)["RETEST_CONFIRMED"]
    assert enough["plus_1r_before_invalidation"] == 1.0
    assert enough["plus_2r_before_invalidation"] == 0.0


# --- Telegram formatting --------------------------------------------------------------


def test_oi_unavailable_renders_cleanly_and_text_is_safe():
    event = early(EventType.FIRST_BREAK, key="ep")
    event.zone = RES
    event.symbol = "<b>X</b>USDT"
    text = format_early_event(event, EarlyContext(), BUILT)
    assert "📊 OI: нет данных" in text and "unavailable" not in text
    assert "&lt;b&gt;X&lt;/b&gt;USDT" in text  # dynamic data escaped
    assert len(text) < 4096
    for banned in ("buy now", "sell now", "enter now", "guaranteed"):
        assert banned not in text.lower()
    assert "Основной HIGH_CONFLUENCE ещё НЕ сформирован." in text


# --- watcher integration --------------------------------------------------------------


class FakeBingX:
    def __init__(self, oi=None, spreads=None):
        self.oi, self.spreads = oi, spreads or {}

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
            for s in ("AAAUSDT", "WIDEUSDT", "THINUSDT")
        ]

    async def tickers(self):
        now = datetime.now(UTC)
        rows = {
            "AAAUSDT": (50e6, "0.01"),
            "WIDEUSDT": (80e6, "1.0"),
            "THINUSDT": (1e5, "0.01"),
        }
        return {
            s: Ticker(
                symbol=s,
                price=100,
                quote_volume=v,
                timestamp=now,
                bid=D(100),
                ask=D(100) + D(spread),
            )
            for s, (v, spread) in rows.items()
        }


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def __call__(self, text, reply_to, buttons):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((text, reply_to, buttons))
        return [700 + len(self.sent)]


def watcher_for(url, sender=None, **settings):
    return FastMarketWatcher(
        Settings(**{"fast_send_telegram": True, "early_send_telegram": True, **settings}),
        get_session_factory(url),
        FakeBingX(),
        sender,
        destination=chat_fingerprint("123456789"),
    )


def first_break_event(symbol="AAAUSDT"):
    engine, tracker = break_engine()
    feed = Feed(engine, tracker).ticks(104.3, 70).ticks(104.8, 20)
    event = next(e for e in feed.events if e.event_type == EventType.FIRST_BREAK)
    event.symbol = symbol
    event.episode.symbol = symbol
    return event


async def test_early_dispatch_captures_message_id_logs_fingerprint_only(
    session, sqlite_database_url, caplog
):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    event = first_break_event()
    with caplog.at_level(logging.INFO):
        await watcher.dispatch(EarlyJob(event, time.time(), time.time()))
    text, _, buttons = sender.sent[0]
    assert text.startswith("⚡ <b>ПЕРВОЕ ПРОБИТИЕ</b>")
    assert "пробой пока intrabar" in text and "📊 OI: нет данных" in text
    assert buttons[0][1].endswith("BINGX:AAAUSDT.P")
    row = session.scalar(select(FastMarketEvent))
    assert (
        row.event_type == "FIRST_BREAK" and row.telegram_message_id == 701 and row.sent
    )
    assert row.episode_id == event.episode.id and row.window is None
    assert row.metrics["invalidation"] < 104.5
    episode = session.get(MarketStructureEpisode, event.episode.id)
    assert episode.phase == "BROKEN" and episode.zone_timeframe == "1h"
    accepted = [r.message for r in caplog.records if "Telegram accepted" in r.message]
    assert accepted and "message_id=701" in accepted[0]
    assert (
        "123456789" not in caplog.text and chat_fingerprint("123456789") in accepted[0]
    )


async def test_early_event_on_active_setup_links_and_keeps_score(
    session, sqlite_database_url
):
    _, _, _, setup = save_ready(session)
    setup.notified_data = {"telegram_message_id": 4242}
    session.commit()
    score = setup.score
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    await watcher.dispatch(EarlyJob(first_break_event(), time.time(), time.time()))
    text, reply_to, _ = sender.sent[0]
    assert reply_to == 4242 and "оценка не меняется" in text
    row = session.scalar(select(FastMarketEvent))
    assert row.market_setup_id == setup.id
    session.expire_all()
    assert session.get(MarketSetup, setup.id).score == score


async def test_high_impact_news_attaches_without_changing_score(
    session, sqlite_database_url, monkeypatch
):
    save_ready(session)
    snapshot = session.scalar(select(ScannerSnapshot))
    snapshot.exchange = "BINGX"
    session.commit()
    before = (snapshot.long_score, snapshot.short_score, dict(snapshot.data))
    news = SimpleNamespace(
        level="HIGH",
        title="Exchange listing",
        received_at=datetime.now(UTC) - timedelta(seconds=42),
    )
    monkeypatch.setattr(
        "app.news.repository.setup_news_context", lambda *a, **k: [news]
    )
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    await watcher.dispatch(EarlyJob(first_break_event(), time.time(), time.time()))
    text = sender.sent[0][0]
    assert "📰 HIGH IMPACT NEWS" in text and f"{before[0]} / 100" in text
    session.expire_all()
    snapshot = session.scalar(select(ScannerSnapshot))
    assert (snapshot.long_score, snapshot.short_score, snapshot.data) == before


async def test_news_failure_never_blocks_an_early_alert(
    sqlite_database_url, monkeypatch
):
    monkeypatch.setattr(
        "app.news.repository.setup_news_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("news down")),
    )
    sender = Recorder()
    await watcher_for(sqlite_database_url, sender).dispatch(
        EarlyJob(first_break_event(), time.time(), time.time())
    )
    assert len(sender.sent) == 1


async def test_restart_restores_antispam_and_open_episodes(
    session, sqlite_database_url
):
    first = watcher_for(sqlite_database_url, Recorder())
    stale = first_break_event("OLDUSDT")  # fixture time: days old
    await first.dispatch(EarlyJob(stale, time.time(), time.time()))
    event = first_break_event()
    event.episode.break_time = event.episode.updated = time.time() - 60
    await first.dispatch(EarlyJob(event, time.time(), time.time()))
    restarted = watcher_for(sqlite_database_url, Recorder())
    restarted.restore_cooldowns()
    assert restarted.counters["restored_episodes"] == 1  # the stale one stays closed
    assert "OLDUSDT" not in restarted.early.episodes
    assert restarted.early.antispam.decide(event, time.time()) is None  # no replay
    restored = restarted.early.episodes["AAAUSDT"][event.episode.key]
    assert restored.id == event.episode.id and restored.phase == "BROKEN"


def test_first_context_pass_primes_silently():
    engine = EarlyEngine(CFG)
    ctx = ignition_context(ignition_bars())
    assert engine.set_context(ctx, 99.9, T0 + 5, prime=True) == []
    assert engine.counters["primed_on_start"] == 1
    assert engine.set_context(ctx, 99.9, T0 + 300) == []  # already known: no replay


async def test_formation_candidate_is_persisted(session, sqlite_database_url):
    bars = triangle_bars()
    ctx = context(bars, zones=(), symbol="AAAUSDT")
    ctx.patterns = [
        p
        for p in detect_patterns(bars, "15m", 1.0, [], CFG)
        if p.pattern_type == "ASCENDING_TRIANGLE"
    ]
    watcher = watcher_for(sqlite_database_url, Recorder())
    events = watcher.early.set_context(ctx, float(bars[-1].close), T0 + 5)
    await watcher.dispatch(EarlyJob(events[0], T0, time.time()))
    row = session.scalar(select(PatternCandidateRecord))
    assert row.pattern_type == "ASCENDING_TRIANGLE" and row.status == "ACTIVE"
    assert row.formation_strength == events[0].strength


async def test_universe_records_every_exclusion_reason(sqlite_database_url):
    watcher = watcher_for(sqlite_database_url)
    universe = await watcher.universe()
    assert universe == ["AAAUSDT"]
    assert watcher.exclusions == {
        "WIDEUSDT": "WIDE_SPREAD",
        "THINUSDT": "LOW_QUOTE_VOLUME",
    }
    assert watcher.spreads["AAAUSDT"] < 0.15


async def test_priority_analysis_is_per_symbol_and_rate_limited(sqlite_database_url):
    async def rescan(symbol):
        return None

    watcher = FastMarketWatcher(
        Settings(early_priority_analyses_per_minute=2),
        get_session_factory(sqlite_database_url),
        FakeBingX(),
        None,
        rescan,
    )
    for symbol in ("AAAUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT"):
        watcher.request_rescan(symbol)
    assert watcher.rescans.qsize() == 2
    assert watcher.counters["fast_priority_analyses_rate_limited"] == 1


async def test_scanner_failure_does_not_crash_the_watcher(sqlite_database_url):
    async def broken(symbol):
        raise RuntimeError("scanner down")

    watcher = FastMarketWatcher(
        Settings(), get_session_factory(sqlite_database_url), FakeBingX(), None, broken
    )
    worker = asyncio.create_task(watcher._rescan_worker())
    watcher.request_rescan("AAAUSDT")
    await asyncio.sleep(0.05)
    watcher.request_rescan("BBBUSDT")
    await asyncio.sleep(0.05)
    assert watcher.counters["fast_rescans_failed:RuntimeError"] == 2
    assert not worker.done()
    worker.cancel()


def test_engine_error_is_isolated_from_the_stream(sqlite_database_url):
    watcher = watcher_for(sqlite_database_url)
    watcher.trackers["AAAUSDT"] = live_tracker()

    def explode(*args):
        raise ValueError("bad context")

    watcher.early.on_tick = explode
    watcher.on_kline("AAA-USDT", int(T0 * 1000), 100.0, 1.0, T0 + 1)
    assert watcher.counters["early_errors:ValueError"] == 1


async def test_stale_stream_is_detected_and_reconnected():
    class Silent:
        sent: ClassVar[list] = []

        async def send_json(self, payload):
            pass

        async def receive(self):
            await asyncio.sleep(3600)

        async def close(self):
            pass

    async def connect():
        return Silent()

    stream = StreamConnection(
        0,
        ["AAA-USDT"],
        lambda *a: None,
        stale_after=0.05,
        max_backoff=0.01,
        connect=connect,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(stream.run(stop))
    await asyncio.sleep(0.4)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert stream.reconnects >= 1


# --- synthetic integration (clearly labelled TEST) -------------------------------------


async def test_synthetic_gas_sequence_ignition_to_retest_confirmed(sqlite_database_url):
    """SYNTHETIC: near 1H support -> ignition -> approach -> first break -> retest ->
    retest confirmed. Every message is labelled TEST / NOT A REAL TRADING SIGNAL."""
    sender = Recorder()
    watcher = FastMarketWatcher(
        Settings(fast_send_telegram=True, early_send_telegram=True),
        get_session_factory(sqlite_database_url),
        FakeBingX(),
        sender,
        mode="TEST",
    )
    support, resistance = (
        zone("SUPPORT", 99.0, 99.5, 2.5),
        zone("RESISTANCE", 104.0, 104.5, 2.5),
    )
    ctx = ignition_context(
        ignition_bars(),
        zones=(support, resistance),
        symbol=SYMBOL,
        oi_change_15m=0.4,
        market={"BTC": "neutral", "ETH": "bullish"},
    )
    ctx.atr["1h"] = 2.5
    tracker = live_tracker()
    watcher.trackers[SYMBOL] = tracker
    for event in watcher.early.set_context(ctx, 99.9, T0 + 5):
        watcher._enqueue_early(event, None)
    sent_types = []

    async def drain():  # the live Telegram worker dispatches continuously
        while watcher.outbox.qsize():
            _, job = await watcher.outbox.get()
            await watcher.dispatch(job)
            sent_types.append(
                job.event.event_type if isinstance(job, EarlyJob) else job.event_type
            )

    await drain()
    at, minute, total = T0 + 10, None, 0.0
    for price, count in (
        (102.0, 70),
        (102.6, 2),
        (103.2, 2),
        (103.8, 3),
        (104.3, 5),
        (104.8, 15),
        (105.3, 10),
        (105.0, 3),
        (104.9, 5),
        (105.45, 5),
        (105.5, 20),
    ):
        for _ in range(count):
            m = int(at // 60 * 60) * 1000
            if m != minute:
                minute, total = m, 0.0
            total += 30
            watcher.clock = lambda at=at: at
            watcher.on_kline("GAS-USDT", m, price, total, at, price, price, price)
            at += 1
            await drain()
    told = [
        "🌱 <b>РАННИЙ LONG-КОНТЕКСТ</b>",
        "🟡 <b>ПОДХОД К ПРОБОЮ</b>",
        "⚡ <b>ПЕРВОЕ ПРОБИТИЕ</b>",
        "🔄 <b>РЕТЕСТ ЗОНЫ</b>",
        "✅ <b>РЕТЕСТ ПОДТВЕРЖДЁН</b>",
    ]
    titles = [
        next((line for line in text.split("\n") if line in told), None)
        for text, _, _ in sender.sent
    ]
    assert [t for t in titles if t in told] == told, titles  # each exactly once
    assert first_seen_types(sent_types)[:1] == [EventType.BULLISH_IGNITION]
    for text, _, _ in sender.sent:
        assert "TEST — NOT A REAL TRADING SIGNAL" in text
        assert "Это НЕ реальный торговый сигнал" in text


# --- regressions from the live run (2026-09-24 21:00: 11 ignitions in 40 s) ---------


def test_ignition_ignores_15m_zones():
    ctx = ignition_context(
        ignition_bars(), zones=(zone("SUPPORT", 99.0, 99.5, tf="15m"),)
    )
    assert EarlyEngine(CFG).set_context(ctx, 99.9, T0 + 5) == []


def test_stale_shift_is_not_an_ignition():
    bars = ignition_bars()
    ctx = ignition_context(bars)
    ctx.breaks5[0] = ctx.breaks5[0].model_copy(
        update={"timestamp": bars[-6].close_time}
    )
    ctx.bars5 = bars + bars_from(
        path([99.95, 99.97, 99.99, 100.0], pad=0.05), end=T0 + 1200
    )
    ctx.built_at = ctx.bars5[-1].close_time
    events = EarlyEngine(CFG).set_context(ctx, 100.0, T0 + 1205)
    assert EventType.BULLISH_IGNITION not in [e.event_type for e in events]


def test_neutral_context_does_not_add_strength():
    ctx = ignition_context(
        ignition_bars(),
        funding_state="NEUTRAL",
        market={"BTC": "neutral", "ETH": "neutral"},
    )
    plain = ignition_context(ignition_bars())
    a = EarlyEngine(CFG).set_context(ctx, 99.9, T0 + 5)[0]
    b = EarlyEngine(CFG).set_context(plain, 99.9, T0 + 5)[0]
    assert a.strength == b.strength and "MARKET_COMPATIBLE" in a.reasons


def test_static_context_alone_never_confirms_a_retest():
    """Regression (live 2026-09-24 RENDERUSDT): OI holding + BTC/ETH compatible
    confirmed a retest 10 s after it started; price/volume action is required."""
    engine, tracker = break_engine(
        oi_change_15m=0.4, market={"BTC": "neutral", "ETH": "neutral"}
    )
    feed = Feed(engine, tracker, volume=10).ticks(104.3, 70).ticks(104.8, 15)
    feed.ticks(105.0, 10).ticks(104.6, 3).ticks(104.55, 5).ticks(104.85, 20)
    assert EventType.RETEST_WATCH in feed.types()
    assert EventType.RETEST_CONFIRMED not in feed.types()


def test_15m_support_resistance_gives_no_realtime_alerts():
    minor = zone("RESISTANCE", 104.0, 104.5, tf="15m")
    engine = engine_with(context(rising_bars(), zones=(minor,)))
    feed = Feed(engine, live_tracker(), volume=30).ticks(102.8, 70).ticks(103.9, 20)
    feed.ticks(104.8, 30)
    assert feed.events == []  # context only; the 1H/4H version alerts (tests above)


def test_5m_formations_stay_internal_evidence():
    bars = triangle_bars()
    ctx = context(bars, zones=())
    ctx.patterns = detect_patterns(bars, "5m", 1.0, [], CFG)
    assert ctx.patterns  # detected ...
    events = EarlyEngine(CFG).set_context(ctx, float(bars[-1].close), T0 + 5)
    assert EventType.FORMATION_WATCH not in [
        e.event_type for e in events
    ]  # ... not sent


def first_seen_types(types):
    return list(dict.fromkeys(types))
