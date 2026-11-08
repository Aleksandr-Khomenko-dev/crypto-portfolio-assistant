"""TradingView QS PRO ICT v4 (ICT Edition v4.2): webhook, worker, filters, message."""

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.scanner_models import (
    Delivery,
    RawWebhook,
    Trade,
    TradeEvent,
    TradingViewEvent,
)
from app.db.session import get_session_factory
from app.macro.calendar import EconomicEvent
from app.public import create_public_app
from app.telegram.tv_ict import EntryContext, format_entry, r_label
from app.tv.ict import (
    check_levels,
    decode,
    detect_format,
    late_minutes,
    parse,
    symbol_allowed,
    timeframe_label,
    timeframe_seconds,
)
from app.tv.worker import TradingViewWorker, scanner_view

TOKEN = "t" * 40
TV_IP = "52.89.214.238"
ENTRY = {  # the sample ENTRY from the specification, verbatim
    "event": "ENTRY",
    "planId": "XAUUSD-5-SELL-1762873500000",
    "symbol": "OANDA:XAUUSD",
    "ticker": "XAUUSD",
    "tf": "5",
    "side": "SELL",
    "model": "Inversion FVG",
    "score": 90,
    "entryType": "market",
    "entry": 3969.80,
    "sl": 3976.80,
    "tp1": 3962.80,
    "tp2": 3959.30,
    "tp3": 3952.00,
    "tp2Name": "London low",
    "tp3Name": "PDL",
    "price": 3969.80,
    "resultR": None,
    "exitPrice": None,
    "units": 35.714286,
    "riskUsd": 250,
    "leverage": 10,
    "barTime": 1762874100000,
    "bornTime": 1762873500000,
    "validHours": 3,
    "biasMain": -1,
    "bias4h": -1,
    "dayRangePct": 72,
    "window": "NY morning window",
    "note": "▶ ENTER NOW at market: reaction candle closed",
    "checklist": "✓D ✓4H ✓BSL PDH ✓disp 1.8x ✓window ✓premium ✓DXY",
}
CLOSE = datetime(2025, 11, 11, 15, 20, tzinfo=UTC)  # barTime 15:15 UTC + 5m


def event(name, **changes):
    return {**ENTRY, "event": name, **changes}


def settings(**overrides):
    base = {
        "tv_webhook_token": TOKEN,
        "telegram_bot_token": "123456:TEST",
        "scanner_telegram_chat_id": "111",
        "calendar_enabled": False,
    }
    return Settings(**{**base, **overrides})


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def __call__(self, chat_id, text, reply_to=None, buttons=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, reply_to))
        return [900 + len(self.sent)]


class Calendar:
    def __init__(self, events, fetched=True):
        self.events = events
        self.fetched_at = CLOSE if fetched else None


def worker(url, sender=None, at=CLOSE + timedelta(seconds=30), calendar=None, **cfg):
    return TradingViewWorker(
        settings(**cfg),
        get_session_factory(url),
        sender,
        calendar=calendar,
        clock=lambda: at,
    )


def store(url, payload, received_at=CLOSE + timedelta(seconds=20), secret_ok=None):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    with get_session_factory(url)() as session:
        row = RawWebhook(
            received_at=received_at, body=body, status="new", secret_ok=secret_ok
        )
        session.add(row)
        session.commit()
        return row.id


# --- parsing ---------------------------------------------------------------------


def test_parse_tolerates_nulls_and_unknown_fields():
    signal = parse({**ENTRY, "brandNewField": {"x": 1}, "tp3": None, "model": None})
    assert signal.plan_id == ENTRY["planId"] and signal.side == "SELL"
    assert signal.tp3 is None and signal.result_r is None
    with pytest.raises(ValidationError):
        parse({k: v for k, v in ENTRY.items() if k != "planId"})


def test_format_detection():
    assert detect_format(ENTRY, legacy_enabled=False) == "v42"
    legacy = {"event_id": "e1", "secret": "x", "event": "SIGNAL"}
    assert detect_format(legacy, legacy_enabled=True) == "legacy"
    assert detect_format(legacy, legacy_enabled=False) is None
    assert detect_format(decode("not json"), legacy_enabled=True) is None


def test_level_validation_buy_and_sell():
    assert check_levels(parse(ENTRY)) is None
    assert "SELL" in check_levels(parse({**ENTRY, "sl": 3960.0}))
    buy = {
        **ENTRY,
        "side": "BUY",
        "sl": 3960.0,
        "tp1": 3980.0,
        "tp2": 3990.0,
        "tp3": None,
    }
    assert check_levels(parse(buy)) is None
    assert "BUY" in check_levels(parse({**buy, "tp1": 3965.0}))
    assert "positive" in check_levels(parse({**ENTRY, "tp2": 0}))


@pytest.mark.parametrize(
    "tf,seconds,label",
    [
        ("5", 300, "5m"),
        ("60", 3600, "1h"),
        ("240", 14400, "4h"),
        ("D", 86400, "1D"),
        ("1D", 86400, "1D"),
        ("W", 604800, "1W"),
        ("30S", 30, "30s"),
        ("x", None, "x"),
    ],
)
def test_timeframes(tf, seconds, label):
    assert timeframe_seconds(tf) == seconds and timeframe_label(tf) == label


def test_freshness_counts_from_the_candle_close():
    signal = parse(ENTRY)
    assert late_minutes(signal, CLOSE + timedelta(minutes=4)) == 4
    assert late_minutes(signal, CLOSE - timedelta(seconds=5)) == 0


def test_symbol_allowlist_ignores_exchange_prefix_and_perp_suffix():
    assert symbol_allowed("OANDA:XAUUSD", "XAUUSD,ETHUSDT")
    assert symbol_allowed("BINGX:ETHUSDT.P", "XAUUSD,ETHUSDT")
    assert not symbol_allowed("BTCUSDT", "XAUUSD,ETHUSDT")
    assert symbol_allowed("ANYUSDT", "")


# --- the entry message ------------------------------------------------------------


def test_entry_message_matches_the_specified_format():
    text = format_entry(
        parse(decode(json.dumps(ENTRY), exact=True)),
        EntryContext(scanner="совпадает", calendar_ok=True),
        settings(),
        CLOSE + timedelta(seconds=30),
    )
    lines = text.split("\n")
    assert lines[0] == "🔴 <b>SELL — ВХОДИТЬ СЕЙЧАС по рынку</b> · XAUUSD · 5m"
    assert lines[1] == "Модель: Inversion FVG · чек-лист 90/100 (это не вероятность)"
    assert lines[2] == "Подтверждение: цена была в зоне, свеча закрылась вниз из зоны"
    for expected in (
        "<pre>Вход:  3969.80  (закрытие свечи 16:20 по Брюсселю)",  # 15:20 UTC
        "Стоп:  3976.80   (риск 7.00)",
        "TP1:   3962.80   (1R — закрыть половину, стоп в безубыток)",
        "TP2:   3959.30   (London low, 1.5R)",
        "TP3:   3952.00   (PDL, ≈2.5R)</pre>",
        "Размер: 35.71 oz = 0.36 лота · риск $250",
        "Сумма при x10: $14 180",
        "✓D ✓4H ✓BSL PDH ✓disp 1.8x ✓window ✓premium ✓DXY",
        "Мой сканер: совпадает",
        "📈 https://www.tradingview.com/chart/?symbol=OANDA:XAUUSD",
    ):
        assert expected in lines, expected
    assert "запоздал" not in text and "НОВОСТИ" not in text
    assert len(text) < 4096


def test_message_background_and_warnings():
    cpi = EconomicEvent("CPI m/m", CLOSE + timedelta(minutes=65))
    context = EntryContext(
        late_minutes=4.2,
        news_now=[cpi],
        next_event=cpi,
        macro={"DXY": {"trend": "UP"}, "US10Y": {"trend": "UP"}, "VIX": {"vix": 16.2}},
    )
    text = format_entry(parse(ENTRY), context, settings(), CLOSE)
    assert "⚠️ сигнал запоздал на 4 мин" in text
    assert "⚠️ <b>НОВОСТИ: осторожно</b> — CPI m/m сегодня 17:25" in text
    assert (
        "📰 Фон: CPI m/m сегодня 17:25 (через 1 ч 5 мин) · DXY ↑ · 10Y ↑ · VIX 16"
        in text
    )
    unavailable = format_entry(
        parse(ENTRY), EntryContext(calendar_ok=False), settings(), CLOSE
    )
    assert "📰 Фон: календарь недоступен" in unavailable


def test_r_labels():
    assert [r_label(x) for x in (1.0, 1.5, 2.542857, 3.0)] == [
        "1R",
        "1.5R",
        "≈2.5R",
        "3R",
    ]


# --- webhook route ------------------------------------------------------------------


@pytest.fixture()
def public_app(sqlite_database_url):
    app = create_public_app(get_settings())
    app.dependency_overrides[get_settings] = lambda: settings()
    return app


async def post(app, path, body, ip=TV_IP, content_type="application/json"):
    transport = httpx.ASGITransport(app=app, client=(ip, 4242))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://bot.test"
    ) as client:
        data = body if isinstance(body, str) else json.dumps(body)
        return await client.post(
            path, content=data, headers={"content-type": content_type}
        )


async def test_webhook_auth_ip_and_fast_reply(public_app, session):
    assert (await post(public_app, "/webhook/tv/wrong-token", ENTRY)).status_code == 404
    assert (
        await post(public_app, f"/webhook/tv/{TOKEN}", ENTRY, ip="8.8.8.8")
    ).status_code == 403
    started = time.perf_counter()
    reply = await post(public_app, f"/webhook/tv/{TOKEN}", ENTRY)
    assert reply.status_code == 200 and time.perf_counter() - started < 3
    rows = session.scalars(select(RawWebhook)).all()
    assert len(rows) == 1 and rows[0].status == "new" and rows[0].client_ip == TV_IP
    assert TOKEN not in rows[0].body


async def test_webhook_disabled_without_token(sqlite_database_url):
    app = create_public_app(get_settings())
    app.dependency_overrides[get_settings] = lambda: settings(tv_webhook_token=None)
    assert (await post(app, f"/webhook/tv/{TOKEN}", ENTRY)).status_code == 404


async def test_webhook_accepts_text_plain_and_limits_size(public_app, session):
    plain = await post(
        public_app, f"/webhook/tv/{TOKEN}", "hello", content_type="text/plain"
    )
    assert plain.status_code == 200
    big = await post(public_app, f"/webhook/tv/{TOKEN}", "x" * 70000)
    assert big.status_code == 413
    assert session.scalar(select(RawWebhook)).body == "hello"


async def test_legacy_secret_is_verified_and_never_stored(public_app, session):
    public_app.dependency_overrides[get_settings] = lambda: settings(
        tradingview_webhook_enabled=True, tradingview_webhook_secret="s" * 24
    )
    legacy = {"event_id": "gold-1", "secret": "s" * 24, "event": "SIGNAL"}
    await post(public_app, f"/webhook/tv/{TOKEN}", legacy)
    row = session.scalar(select(RawWebhook))
    assert row.secret_ok is True and "s" * 24 not in row.body and '"***"' in row.body


# --- worker ---------------------------------------------------------------------------


async def test_entry_is_sent_once_to_the_owner(sqlite_database_url, session):
    sender = Recorder()
    store(sqlite_database_url, ENTRY)
    store(sqlite_database_url, ENTRY)  # TradingView retry / duplicate alert
    assert await worker(sqlite_database_url, sender).process_pending() == 2
    assert len(sender.sent) == 1
    chat, text, _ = sender.sent[0]
    assert chat == "111" and text.startswith("🔴 <b>SELL — ВХОДИТЬ СЕЙЧАС")
    statuses = [
        r.status for r in session.scalars(select(RawWebhook).order_by(RawWebhook.id))
    ]
    assert statuses == ["processed", "duplicate"]
    trade = session.scalar(select(Trade))
    assert (
        trade.status == "OPEN"
        and trade.source == "tv_ict_v42"
        and trade.ticker == "XAUUSD"
    )
    delivery = session.scalar(select(Delivery))
    assert delivery.message_id == 901 and delivery.audience == "owner"
    assert session.scalar(select(TradeEvent)).notified


async def test_owner_chat_overrides_the_scanner_chat(sqlite_database_url):
    sender = Recorder()
    store(sqlite_database_url, ENTRY)
    await worker(sqlite_database_url, sender, owner_chat_id="777").process_pending()
    assert sender.sent[0][0] == "777"


async def test_zone_to_entry_to_exit_only_entry_reaches_the_chat(
    sqlite_database_url, session
):
    sender = Recorder()
    for payload in (
        event("ZONE_WAIT"),
        ENTRY,
        event("TP1", resultR=0.5, exitPrice=3962.8),
        event("REST_CLOSED", resultR=0.0, exitPrice=3969.8),
    ):
        store(sqlite_database_url, payload)
    await worker(sqlite_database_url, sender).process_pending()
    assert len(sender.sent) == 1 and "ВХОДИТЬ СЕЙЧАС" in sender.sent[0][1]
    trade = session.scalar(select(Trade))
    assert trade.status == "CLOSED" and trade.result_r == pytest.approx(0.5)
    events = [
        e.event for e in session.scalars(select(TradeEvent).order_by(TradeEvent.id))
    ]
    assert events == ["ZONE_WAIT", "ENTRY", "TP1", "REST_CLOSED"]


async def test_zone_then_cancelled_sends_nothing(sqlite_database_url, session):
    sender = Recorder()
    other = {"planId": "XAUUSD-5-BUY-1", "side": "BUY"}
    store(sqlite_database_url, event("ZONE_WAIT", **other))
    store(sqlite_database_url, event("CANCELLED", **other))
    await worker(sqlite_database_url, sender).process_pending()
    assert sender.sent == []
    assert session.scalar(select(Trade)).status == "CANCELLED"


async def test_late_entry_is_stored_not_sent(sqlite_database_url, session):
    sender = Recorder()
    store(sqlite_database_url, ENTRY, received_at=CLOSE + timedelta(minutes=11))
    await worker(sqlite_database_url, sender).process_pending()
    assert sender.sent == [] and session.scalar(select(Trade)).status == "LATE"


async def test_slightly_late_entry_is_sent_with_a_warning(sqlite_database_url):
    sender = Recorder()
    store(sqlite_database_url, ENTRY, received_at=CLOSE + timedelta(minutes=5))
    await worker(sqlite_database_url, sender).process_pending()
    assert "⚠️ сигнал запоздал на 5 мин" in sender.sent[0][1]


async def test_low_score_filtered_and_symbol_ignored(sqlite_database_url, session):
    sender = Recorder()
    store(sqlite_database_url, event("ENTRY", score=70))
    store(
        sqlite_database_url,
        event("ENTRY", planId="BTC-1", ticker="BTCUSDT", symbol="BINGX:BTCUSDT"),
    )
    await worker(sqlite_database_url, sender).process_pending()
    assert sender.sent == []
    assert session.scalar(select(Trade)).status == "FILTERED"
    ignored = session.scalars(
        select(RawWebhook).where(RawWebhook.status == "ignored")
    ).all()
    assert len(ignored) == 1 and ignored[0].error == "symbol not allowed"


async def test_invalid_levels_and_garbage_are_logged_not_sent(
    sqlite_database_url, session
):
    sender = Recorder()
    store(sqlite_database_url, event("ENTRY", sl=3960.0))  # SELL with stop below entry
    store(sqlite_database_url, "not json at all")
    store(sqlite_database_url, {k: v for k, v in ENTRY.items() if k != "side"})
    await worker(sqlite_database_url, sender).process_pending()
    assert sender.sent == []
    assert session.scalar(select(Trade)).status == "INVALID"
    statuses = sorted(r.status for r in session.scalars(select(RawWebhook)))
    assert statuses == ["invalid", "invalid", "processed"]


async def test_news_warning_uses_the_calendar(sqlite_database_url):
    sender = Recorder()
    store(sqlite_database_url, ENTRY)
    calendar = Calendar(
        [EconomicEvent("FOMC Statement", CLOSE + timedelta(minutes=20))]
    )
    await worker(sqlite_database_url, sender, calendar=calendar).process_pending()
    assert "НОВОСТИ: осторожно</b> — FOMC Statement" in sender.sent[0][1]


async def test_without_telegram_the_signal_is_still_stored(
    sqlite_database_url, session
):
    store(sqlite_database_url, ENTRY)
    await worker(sqlite_database_url, None).process_pending()
    trade_event = session.scalar(select(TradeEvent))
    assert trade_event.notify_reason == "telegram not configured"
    assert session.scalar(select(Trade)).status == "OPEN"


async def test_telegram_failure_is_recorded_and_not_retried(
    sqlite_database_url, session
):
    sender = Recorder(fail=True)
    store(sqlite_database_url, ENTRY)
    run = worker(sqlite_database_url, sender)
    await run.process_pending()
    await run.process_pending()
    delivery = session.scalar(select(Delivery))
    assert delivery.error == "RuntimeError" and delivery.message_id is None
    assert not session.scalar(select(TradeEvent)).notified


async def test_legacy_format_through_the_new_route(sqlite_database_url, session):
    legacy = {
        "secret": "***",
        "event_id": "gold-7",
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "event": "BOS",
        "direction": "LONG",
        "price": "100",
        "timestamp": (CLOSE + timedelta(seconds=10)).isoformat(),
    }
    store(sqlite_database_url, legacy, secret_ok=True)
    store(sqlite_database_url, {**legacy, "event_id": "gold-8"}, secret_ok=False)
    run = worker(
        sqlite_database_url,
        Recorder(),
        tradingview_webhook_enabled=True,
        tradingview_webhook_secret="s" * 24,
    )
    await run.process_pending()
    assert session.get(TradingViewEvent, "gold-7") is not None
    assert session.get(TradingViewEvent, "gold-8") is None
    statuses = [
        r.status for r in session.scalars(select(RawWebhook).order_by(RawWebhook.id))
    ]
    assert statuses == ["processed", "invalid"]


async def test_legacy_disabled_rejects_old_format(sqlite_database_url, session):
    store(sqlite_database_url, {"event_id": "g", "secret": "***"}, secret_ok=True)
    await worker(
        sqlite_database_url, Recorder(), legacy_tv_format_enabled=False
    ).process_pending()
    assert session.scalar(select(RawWebhook)).status == "invalid"


# --- scanner as background ------------------------------------------------------------


def test_scanner_view_words():
    now = datetime.now(UTC)
    setup = lambda d, score, ready="WAIT_STRUCTURE": SimpleNamespace(
        direction=d, score=score, readiness=ready
    )
    result = lambda *setups: SimpleNamespace(created_at=now, setups=list(setups))
    s = settings()
    assert scanner_view("SHORT", None, s, now) == "нет данных"
    assert (
        scanner_view("SHORT", result(setup("SHORT", 72), setup("LONG", 40)), s, now)
        == "совпадает"
    )
    assert (
        scanner_view("SHORT", result(setup("SHORT", 40), setup("LONG", 75)), s, now)
        == "против"
    )
    assert (
        scanner_view("SHORT", result(setup("SHORT", 55), setup("LONG", 50)), s, now)
        == "нейтрально"
    )
    stale = SimpleNamespace(created_at=now - timedelta(hours=2), setups=[])
    assert scanner_view("SHORT", stale, s, now) == "нет данных"


def test_scanner_alerts_flag_silences_scan_notifications():
    from app.services.scanner_service import RunNotifier

    on = RunNotifier(SimpleNamespace(settings=Settings()), datetime.now(UTC))
    off = RunNotifier(
        SimpleNamespace(settings=Settings(scanner_alerts_enabled=False)),
        datetime.now(UTC),
    )
    assert on.enabled and not off.enabled


async def test_scanner_alerts_flag_silences_fast_and_early_alerts(sqlite_database_url):
    from app.fast.detector import FastState
    from app.fast.watcher import FastJob, FastMarketWatcher
    from tests.test_fast_watcher import FakeBingX, trig
    from tests.test_fast_watcher import Recorder as FastRecorder

    sender = FastRecorder()
    watcher = FastMarketWatcher(
        Settings(scanner_alerts_enabled=False),
        get_session_factory(sqlite_database_url),
        FakeBingX(),
        sender,
    )
    extreme = trig(FastState.EXTREME_MOVE)
    await watcher.dispatch(FastJob("AAAUSDT", extreme, "NEW", time.time(), time.time()))
    assert sender.sent == []  # detected and stored, not sent


def test_replayed_test_plans_are_labelled_as_tests():
    text = format_entry(
        parse({**ENTRY, "planId": "TEST-XAUUSD-5-SELL-1"}),
        EntryContext(),
        settings(),
        CLOSE,
    )
    assert text.startswith("🧪 <b>TEST — NOT A REAL TRADING SIGNAL</b>")
    real = format_entry(parse(ENTRY), EntryContext(), settings(), CLOSE)
    assert "TEST" not in real.split("\n")[0]
