"""Notification policy: detection/persistence stay complete, Telegram is selective."""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from sqlalchemy import select

from app.db.scanner_models import FastMarketEvent
from app.early.model import EarlyEvent, EventType
from app.early.policy import NotificationPolicy, PolicyConfig
from app.early.service import EarlyOutcomeService
from app.fast.detector import FastState
from app.fast.watcher import EarlyJob, FastJob
from tests.test_early_engine import CFG as ENGINE_CFG
from tests.test_early_engine import (
    RES,
    SUP,
    SYMBOL,
    T0,
    EarlyEngine,
    Feed,
    Recorder,
    break_engine,
    first_break_event,
    ignition_bars,
    ignition_context,
    watcher_for,
    zone,
)
from tests.test_fast_watcher import trig

NOW = time.time()


def event(event_type, zone_=RES, reasons=(), strength=60, **metrics):
    return EarlyEvent(
        "AAAUSDT",
        event_type,
        "LONG",
        104.0,
        NOW,
        zone=zone_,
        strength=strength,
        reasons=list(reasons),
        metrics=metrics,
    )


def rows(session):
    session.expire_all()
    return session.scalars(
        select(FastMarketEvent).order_by(FastMarketEvent.detected_at)
    ).all()


async def dispatch(watcher, ev, at=None):
    at = at or time.time()
    await watcher.dispatch(EarlyJob(ev, at, at))


# --- research-only events ---------------------------------------------------------


async def test_zone_and_formation_watch_persist_but_never_notify(
    session, sqlite_database_url
):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    await dispatch(watcher, event(EventType.ZONE_WATCH))
    await dispatch(
        watcher, event(EventType.FORMATION_WATCH, reasons=["ASCENDING_TRIANGLE"])
    )
    assert sender.sent == []
    stored = rows(session)
    assert [r.event_type for r in stored] == ["ZONE_WATCH", "FORMATION_WATCH"]
    assert all(not r.sent and r.context["notify"]["reason"] == "policy" for r in stored)
    stats = watcher.policy.snapshot()
    assert stats["ZONE_WATCH"]["persisted"] == 1
    assert stats["ZONE_WATCH"]["suppressed_by_policy"] == 1
    assert stats["FORMATION_WATCH"]["sent"] == 0


async def test_late_extended_move_is_persisted_and_silent(session, sqlite_database_url):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    await dispatch(watcher, event(EventType.LATE_EXTENDED_MOVE, zone_=None))
    assert sender.sent == []
    assert rows(session)[0].context["notify"]["reason"] == "late"
    assert watcher.policy.snapshot()["LATE_EXTENDED_MOVE"]["suppressed_late"] == 1


# --- ignition -----------------------------------------------------------------------


def test_qualified_ignition_sends_weak_ignition_does_not():
    policy = NotificationPolicy()
    strong = event(
        EventType.BULLISH_IGNITION, SUP, ["CHOCH_5M", "HIGHER_LOW", "VOLUME_EXPANSION"]
    )
    weak = event(
        EventType.BULLISH_IGNITION,
        SUP,
        ["CHOCH_5M", "HIGHER_LOW", "MOMENTUM_DECELERATION"],
    )
    reclaim = event(
        EventType.BULLISH_IGNITION, SUP, ["LIQUIDITY_SWEEP", "ZONE_RECLAIM"]
    )
    minor = event(
        EventType.BULLISH_IGNITION,
        zone("SUPPORT", 99, 99.5, tf="15m"),
        ["CHOCH_5M", "VOLUME_EXPANSION"],
    )
    assert policy.decide_early(strong, NOW).send
    assert policy.decide_early(reclaim, NOW).send  # strong reclaim counts
    decision = policy.decide_early(weak, NOW)
    assert not decision.send and decision.reason == "policy"
    assert not policy.decide_early(minor, NOW).send  # not a 1H/4H zone


def test_weak_ignition_is_still_detected_for_research():
    bars = ignition_bars()
    bars = [b.model_copy(update={"low": max(b.low, D("99.05"))}) for b in bars]
    ctx = ignition_context(bars)  # shift at 1H support, but no sweep ...
    ctx.rvol5 = 1.0  # ... and no volume/OI: recorded for research, not told
    events = EarlyEngine(ENGINE_CFG).set_context(ctx, 99.9, T0 + 5)
    assert [e.event_type for e in events] == [EventType.BULLISH_IGNITION]
    assert not NotificationPolicy().decide_early(events[0], T0 + 5).send


# --- approach ----------------------------------------------------------------------


async def test_wide_approach_persists_close_qualified_approach_sends(
    session, sqlite_database_url
):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    wide = event(
        EventType.BREAKOUT_APPROACH,
        reasons=["VOLUME_RISING"],
        distance_to_level_atr=0.30,
    )
    close = event(
        EventType.BREAKOUT_APPROACH,
        reasons=["VOLUME_RISING"],
        distance_to_level_atr=0.15,
    )
    bare = event(EventType.BREAKOUT_APPROACH, reasons=[], distance_to_level_atr=0.10)
    await dispatch(watcher, wide)
    assert (
        sender.sent == [] and rows(session)[0].context["notify"]["reason"] == "policy"
    )
    assert not watcher.policy.decide_early(
        bare, NOW
    ).send  # needs a supporting condition
    await dispatch(watcher, close)
    assert len(sender.sent) == 1 and sender.sent[0][0].startswith("🟡")
    assert len(rows(session)) == 2  # both persisted


def test_engine_records_close_approach_after_a_wide_one():
    """The close band is its own detection record (not swallowed as a duplicate)."""
    engine, tracker = break_engine()
    engine.contexts[SYMBOL].zones = [RES]
    feed = (
        Feed(engine, tracker, volume=30)
        .ticks(103.0, 70)
        .ticks(103.7, 20)
        .ticks(103.85, 20)
    )
    approaches = [e for e in feed.events if e.event_type == EventType.BREAKOUT_APPROACH]
    assert [round(e.metrics["distance_to_level_atr"], 2) for e in approaches] == [
        0.3,
        0.15,
    ]


# --- breakout episode ------------------------------------------------------------------


async def test_first_break_retest_watch_and_confirmed_all_send_threaded(
    session, sqlite_database_url
):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    engine, tracker = break_engine(oi_change_15m=0.4, market={"BTC": "neutral"})
    feed = Feed(engine, tracker, volume=30).ticks(104.3, 70).ticks(104.8, 15)
    feed.ticks(105.0, 10).ticks(104.6, 3).ticks(104.55, 5).ticks(104.85, 5).ticks(
        104.9, 30
    )
    wanted = [EventType.FIRST_BREAK, EventType.RETEST_WATCH, EventType.RETEST_CONFIRMED]
    for ev in feed.events:
        ev.symbol = "AAAUSDT"
        if ev.event_type in wanted:
            await dispatch(watcher, ev)
    titles = [text.split("\n")[0] for text, _, _ in sender.sent]
    assert titles == [
        "⚡ <b>ПЕРВОЕ ПРОБИТИЕ</b>",
        "🔄 <b>РЕТЕСТ ЗОНЫ</b>",
        "✅ <b>РЕТЕСТ ПОДТВЕРЖДЁН</b>",
    ]
    first_id = 701
    assert [reply for _, reply, _ in sender.sent] == [None, first_id, first_id]


async def test_failed_breakout_only_if_the_user_saw_the_breakout(sqlite_database_url):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    unseen = first_break_event()
    failed = event(EventType.FAILED_BREAKOUT, reasons=["LEVEL_LOST"])
    failed.episode = unseen.episode
    await dispatch(watcher, failed)
    assert sender.sent == []  # never told about that breakout
    seen = first_break_event()
    await dispatch(watcher, seen)
    failed_seen = event(EventType.FAILED_BREAKOUT, reasons=["LEVEL_LOST"])
    failed_seen.episode = seen.episode
    await dispatch(watcher, failed_seen)
    assert [t.split("\n")[0] for t, _, _ in sender.sent] == [
        "⚡ <b>ПЕРВОЕ ПРОБИТИЕ</b>",
        "❌ <b>ЛОЖНЫЙ ПРОБОЙ</b>",
    ]


async def test_failed_breakout_rule_survives_restart(sqlite_database_url):
    first = watcher_for(sqlite_database_url, Recorder())
    seen = first_break_event()
    seen.episode.break_time = seen.episode.updated = time.time()
    await dispatch(first, seen)
    restarted = watcher_for(sqlite_database_url, Recorder())
    restarted.restore_cooldowns()
    failed = event(EventType.FAILED_BREAKOUT, reasons=["LEVEL_LOST"])
    failed.episode = seen.episode
    assert restarted.policy.decide_early(failed, time.time()).send


# --- fast moves ---------------------------------------------------------------------


async def test_standalone_fast_move_is_silent_but_persisted(
    session, sqlite_database_url
):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    await watcher.dispatch(FastJob("AAAUSDT", trig(), "NEW", time.time(), time.time()))
    assert sender.sent == []
    row = rows(session)[0]
    assert row.event_type == "FAST_MOVE" and not row.sent
    assert row.context["notify"]["detail"] == "standalone fast move"


async def test_fast_move_at_structure_or_break_notifies(sqlite_database_url):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    with_break = FastJob(
        "AAAUSDT",
        trig(),
        "NEW",
        time.time(),
        time.time(),
        extra_reasons=["BREAKOUT_ACCELERATION"],
    )
    await watcher.dispatch(with_break)
    assert len(sender.sent) == 1
    policy = NotificationPolicy()
    at_htf = policy.decide_fast(
        "X", "LONG", "FAST_MOVE", "NEW", [], NOW, active_setup=False, near_htf=True
    )
    on_setup = policy.decide_fast(
        "X", "LONG", "FAST_MOVE", "NEW", [], NOW, active_setup=True, near_htf=False
    )
    assert at_htf.send and on_setup.send


async def test_momentum_and_extreme_send_once(sqlite_database_url):
    sender = Recorder()
    watcher = watcher_for(sqlite_database_url, sender)
    confirmed = trig(FastState.CONFIRMED_MOMENTUM, 3.2)
    await watcher.dispatch(
        FastJob("AAAUSDT", confirmed, "UPGRADE", time.time(), time.time())
    )
    await watcher.dispatch(
        FastJob(
            "AAAUSDT",
            trig(FastState.CONFIRMED_MOMENTUM, 5.0),
            "EXTENSION",
            time.time(),
            time.time(),
        )
    )
    assert len(sender.sent) == 1  # the extension of the same move stays silent
    policy = NotificationPolicy()
    ev = event(EventType.MOMENTUM_CONFIRMED, reasons=["VOLUME_ANOMALY"])
    assert policy.decide_early(ev, NOW).send
    policy.record_sent(ev, "AAAUSDT", "LONG", "MOMENTUM_CONFIRMED", 1, NOW)
    again = policy.decide_early(event(EventType.MOMENTUM_CONFIRMED), NOW + 60)
    assert not again.send and again.reason == "duplicate"


def test_extreme_move_already_represented_by_a_break_is_not_repeated():
    policy = NotificationPolicy()
    brk = event(EventType.FIRST_BREAK)
    policy.record_sent(brk, "AAAUSDT", "LONG", "FIRST_BREAK", 10, NOW)
    decision = policy.decide_fast(
        "AAAUSDT",
        "LONG",
        "EXTREME_MOVE",
        "UPGRADE",
        [],
        NOW + 60,
        active_setup=False,
        near_htf=False,
    )
    assert not decision.send and decision.reason == "duplicate"
    elsewhere = policy.decide_fast(
        "BBBUSDT",
        "LONG",
        "EXTREME_MOVE",
        "NEW",
        [],
        NOW,
        active_setup=False,
        near_htf=False,
    )
    assert elsewhere.send


# --- episode dedupe / threading / rate ------------------------------------------------


def test_overlapping_labels_at_one_level_make_one_user_episode():
    policy = NotificationPolicy()
    triangle = event(
        EventType.FORMATION_WATCH,
        zone("RESISTANCE", 104.4, 104.4, tf="15m"),
        ["ASCENDING_TRIANGLE"],
    )
    compression = event(
        EventType.FORMATION_WATCH,
        zone("RESISTANCE", 104.45, 104.45, tf="15m"),
        ["BREAKOUT_COMPRESSION"],
    )
    approach = event(
        EventType.BREAKOUT_APPROACH, RES, ["COMPRESSION"], distance_to_level_atr=0.1
    )
    sent = []
    for ev in (triangle, compression, approach):
        decision = policy.decide_early(ev, NOW)
        if decision.send:
            sent.append(ev.event_type)
            policy.record_sent(ev, "AAAUSDT", "LONG", ev.event_type.value, 1, NOW)
    assert sent == [EventType.BREAKOUT_APPROACH]
    # A second overlapping breakout of the same level (pattern boundary + S/R zone)
    # is one user-facing break; its retest stays hidden too.
    first = first_break_event()
    assert policy.decide_early(first, NOW).send
    policy.record_sent(first, "AAAUSDT", "LONG", "FIRST_BREAK", 2, NOW)
    overlap = first_break_event()
    overlap.zone = zone("RESISTANCE", 104.45, 104.55, tf="15m")
    overlap.zone = overlap.zone.__class__(
        **{**overlap.zone.__dict__, "source": "PATTERN"}
    )
    assert policy.decide_early(overlap, NOW + 30).reason == "duplicate"
    retest = event(EventType.RETEST_WATCH, overlap.zone)
    retest.episode = overlap.episode
    assert policy.decide_early(retest, NOW + 90).reason == "duplicate"
    lower_stage = event(
        EventType.BREAKOUT_APPROACH, RES, ["COMPRESSION"], distance_to_level_atr=0.1
    )
    assert policy.decide_early(lower_stage, NOW + 120).reason == "duplicate"


def test_pre_event_alerts_are_smoothed_but_breaks_never_limited():
    policy = NotificationPolicy(PolicyConfig(budget_per_5min=2, budget_per_hour=12))
    for i in range(2):
        ev = event(
            EventType.BREAKOUT_APPROACH,
            zone("RESISTANCE", 100 + 10 * i, 100.5 + 10 * i),
            ["VOLUME_RISING"],
            distance_to_level_atr=0.1,
        )
        ev.symbol = f"S{i}USDT"
        assert policy.decide_early(ev, NOW + i).send
        policy.record_sent(ev, ev.symbol, "LONG", "BREAKOUT_APPROACH", i, NOW + i)
    third = event(
        EventType.BREAKOUT_APPROACH,
        reasons=["VOLUME_RISING"],
        distance_to_level_atr=0.1,
    )
    third.symbol = "S9USDT"
    assert policy.decide_early(third, NOW + 3).reason == "rate_limit"
    for i in range(5):
        brk = first_break_event(f"B{i}USDT")
        assert policy.decide_early(brk, NOW + 4).send  # FIRST_BREAK never capped
        policy.record_sent(brk, brk.symbol, "LONG", "FIRST_BREAK", 50 + i, NOW + 4)
    assert policy.decide_early(third, NOW + 400).send  # budget refills


# --- research dataset is not reduced ---------------------------------------------------


async def test_suppressed_events_still_enter_outcome_research(
    session, sqlite_database_url
):
    watcher = watcher_for(sqlite_database_url, Recorder())
    old = time.time() - 6 * 3600
    await dispatch(watcher, event(EventType.ZONE_WATCH), at=old)
    await dispatch(
        watcher, event(EventType.FORMATION_WATCH, reasons=["BULL_FLAG"]), at=old
    )
    await watcher.dispatch(FastJob("AAAUSDT", trig(), "NEW", old, old))
    stored = rows(session)
    assert [r.sent for r in stored] == [False, False, False]
    service = EarlyOutcomeService(watcher.settings, watcher.sessions, None)
    due = service._due(datetime.now(UTC) + timedelta(minutes=1), 50)
    assert {r.event_type for r in due} == {"ZONE_WATCH", "FORMATION_WATCH", "FAST_MOVE"}


def test_fast_move_after_a_shown_break_is_represented_by_it():
    """Regression (live 2026-09-25 SEIUSDT): FAST_MOVE sent right after the break and
    retest of the same move; the stronger event already carries it."""
    policy = NotificationPolicy()
    brk = event(EventType.FIRST_BREAK)
    policy.record_sent(brk, "AAAUSDT", "LONG", "FIRST_BREAK", 10, NOW)
    decision = policy.decide_fast(
        "AAAUSDT",
        "LONG",
        "FAST_MOVE",
        "NEW",
        ["BREAKOUT_ACCELERATION"],
        NOW + 20,
        active_setup=False,
        near_htf=True,
    )
    assert not decision.send and decision.reason == "duplicate"
    later = policy.decide_fast(
        "AAAUSDT",
        "LONG",
        "FAST_MOVE",
        "NEW",
        [],
        NOW + 2000,
        active_setup=False,
        near_htf=True,
    )
    assert later.send  # a new move long after the break is its own story


def test_fast_move_seconds_after_any_alert_is_the_same_story():
    """Regression (live 2026-09-25 XPLUSDT): approach sent, then a FAST_MOVE of the
    same symbol and direction 2 s later."""
    policy = NotificationPolicy()
    approach = event(
        EventType.BREAKOUT_APPROACH,
        reasons=["VOLUME_RISING"],
        distance_to_level_atr=0.1,
    )
    policy.record_sent(approach, "AAAUSDT", "LONG", "BREAKOUT_APPROACH", 7, NOW)
    same = policy.decide_fast(
        "AAAUSDT",
        "LONG",
        "FAST_MOVE",
        "NEW",
        [],
        NOW + 2,
        active_setup=True,
        near_htf=False,
    )
    assert not same.send and same.reason == "duplicate"
    momentum = policy.decide_fast(
        "AAAUSDT",
        "LONG",
        "CONFIRMED_MOMENTUM",
        "UPGRADE",
        [],
        NOW + 2,
        active_setup=False,
        near_htf=False,
    )
    assert momentum.send  # a genuine upgrade still goes out (once)
    opposite = policy.decide_fast(
        "AAAUSDT",
        "SHORT",
        "FAST_MOVE",
        "NEW",
        [],
        NOW + 2,
        active_setup=True,
        near_htf=False,
    )
    assert opposite.send
