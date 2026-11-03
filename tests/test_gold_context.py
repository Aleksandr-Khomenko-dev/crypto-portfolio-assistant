"""Gold macro context (DXY/US10Y/VIX), economic calendar, macro news, TradingView link."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.macro.calendar import EconomicCalendar, parse_extra, parse_forexfactory
from app.macro.context import gold_macro_context
from app.macro.reminders import remind_once
from app.macro.service import GoldMacroService
from app.macro.sources import (
    parse_treasury_10y,
    parse_vix_history,
    parse_vix_quote,
)
from app.news.classify import classify, importance, verification
from app.news.domain import EventType, Freshness, NormalizedNewsItem, SourceType
from app.news.entities import NewsEntityResolver, load_coverage
from app.news.providers.sources import RssProvider, default_providers
from app.news.providers.telegram_channels import channel_item
from app.scanner.domain import Candle, Derivatives, Direction, MarketContext
from app.scanner.scoring import score_setup
from app.services.scanner_service import symbol_context
from app.telegram.calendar import format_reminder
from tests.test_news_pipeline import Recorder, pipeline_for, until
from tests.test_scanner_scoring import aligned_frames

GOLD = "NCCOGOLD2USDUSDT"
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

TREASURY_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns="http://www.w3.org/2005/Atom">
{entries}
</feed>"""
ENTRY = """<entry><content type="application/xml"><m:properties>
<d:NEW_DATE m:type="Edm.DateTime">{day}T00:00:00</d:NEW_DATE>
<d:BC_10YEAR m:type="Edm.Double">{value}</d:BC_10YEAR>
</m:properties></content></entry>"""


def treasury(values):
    start = date(2026, 9, 1)
    return TREASURY_XML.format(
        entries="".join(
            ENTRY.format(day=(start + timedelta(days=i)).isoformat(), value=v)
            for i, v in enumerate(values)
        )
    )


def bars(closes, start=T0 - timedelta(days=30)):
    out = []
    for i, close in enumerate(closes):
        opened = start + timedelta(hours=4 * i)
        price = Decimal(str(close))
        out.append(
            Candle(
                open_time=opened,
                close_time=opened + timedelta(hours=4, milliseconds=-1),
                open=price,
                high=price + Decimal("0.1"),
                low=price - Decimal("0.1"),
                close=price,
                volume=Decimal(1),
            )
        )
    return out


RISING = [100 + i * 0.1 for i in range(80)]
FALLING = [100 - i * 0.1 for i in range(80)]
YIELDS_UP = [(date(2026, 9, 1) + timedelta(days=i), 4.0 + i * 0.02) for i in range(40)]
YIELDS_DOWN = [(d, 6.0 - (v - 4.0)) for d, v in YIELDS_UP]


# --- sources ---------------------------------------------------------------------


def test_treasury_cboe_parsers():
    rows = parse_treasury_10y(treasury([4.5, 4.6, ""]))
    assert rows == [(date(2026, 9, 1), 4.5), (date(2026, 9, 2), 4.6)]
    history = parse_vix_history(
        "DATE,OPEN,HIGH,LOW,CLOSE\n11/02/2026,19.05,21.77,18.03,21.15\nbad,row\n"
    )
    assert history == [(date(2026, 11, 2), 21.15)]
    assert parse_vix_quote({"data": {"current_price": 20.92}}) == 20.92


# --- macro rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dxy", "yields", "state"),
    [
        (RISING, YIELDS_UP, "BEARISH"),  # dollar + yields up: headwind for gold LONG
        (FALLING, YIELDS_DOWN, "BULLISH"),  # both down: headwind for gold SHORT
        (RISING, YIELDS_DOWN, "MIXED"),
        (None, None, "unavailable"),
    ],
)
def test_gold_macro_state_follows_dollar_and_yields(dxy, yields, state):
    context = gold_macro_context(
        bars(dxy) if dxy else None, yields, 18.0, [], Settings()
    )
    assert context.kind == "GOLD_MACRO" and context.state == state
    assert not context.risk_off


def test_vix_spike_or_panic_is_risk_off():
    calm = [(date(2026, 10, 1) + timedelta(days=i), 15.0) for i in range(20)]
    assert gold_macro_context(None, YIELDS_UP, 18.5, calm, Settings()).risk_off
    assert not gold_macro_context(None, YIELDS_UP, 16.0, calm, Settings()).risk_off
    assert gold_macro_context(None, YIELDS_UP, 31.0, [], Settings()).risk_off


def scored(context, direction=Direction.LONG, **settings):
    data = Derivatives(
        funding_state="NEUTRAL", interpretation="NEW_LONG_PARTICIPATION_POSSIBLE"
    )
    return score_setup(
        GOLD, direction, aligned_frames(), data, context, Settings(**settings)
    )


def test_macro_and_event_gate_gold_readiness():
    supportive = scored(MarketContext(kind="GOLD_MACRO", state="BULLISH"))
    assert supportive.readiness == "READY" and supportive.blocks["market_context"] == 5
    against = scored(MarketContext(kind="GOLD_MACRO", state="BEARISH"))
    assert against.readiness == "WAIT_MACRO" and against.blocks["market_context"] == 0
    assert (
        scored(
            MarketContext(kind="GOLD_MACRO", state="BEARISH"), macro_veto=False
        ).readiness
        == "READY"
    )
    # VIX risk-off blocks gold SHORTs only.
    assert (
        scored(
            MarketContext(kind="GOLD_MACRO", state="BULLISH", risk_off=True)
        ).readiness
        == "READY"
    )
    event = scored(
        MarketContext(
            kind="GOLD_MACRO", state="BULLISH", event="Non-Farm Employment Change"
        )
    )
    assert event.readiness == "WAIT_EVENT" and "Non-Farm" in event.next_condition


def test_gold_never_uses_btc_context():
    btc = MarketContext(state="RISK_OFF")
    assert symbol_context("BTCUSDT", btc, None, []) is btc
    gold = symbol_context(GOLD, btc, None, [])
    assert gold.kind == "GOLD_MACRO" and gold.state == "unavailable"
    nfp = parse_forexfactory(FF_ROWS, {"USD"}, "High")
    paused = symbol_context(
        GOLD, btc, MarketContext(kind="GOLD_MACRO", state="MIXED"), nfp
    )
    assert paused.event and paused.event_at == nfp[0].at


class DXYProvider:
    async def candles(self, symbol, timeframe, now):
        assert (symbol, timeframe) == ("NCSIDXY2USDUSDT", "4h")
        return bars(RISING)


def macro_transport(request: httpx.Request) -> httpx.Response:
    if "treasury.gov" in request.url.host:
        return httpx.Response(200, text=treasury([4.0 + i * 0.02 for i in range(40)]))
    if request.url.path.endswith("_VIX.json"):
        return httpx.Response(200, json={"data": {"current_price": 17.0}})
    return httpx.Response(200, text="DATE,OPEN,HIGH,LOW,CLOSE\n11/02/2026,1,1,1,16.0\n")


async def test_macro_service_combines_sources_and_reports_failures():
    client = httpx.AsyncClient(transport=httpx.MockTransport(macro_transport))
    service = GoldMacroService(Settings(), DXYProvider(), client)
    context = await service.context(T0)
    assert context.state == "BEARISH" and context.assets["VIX"]["vix"] == 17.0
    assert "errors" not in context.assets

    def down(request):
        raise httpx.ConnectError("down")

    broken = GoldMacroService(
        Settings(),
        DXYProvider(),
        httpx.AsyncClient(transport=httpx.MockTransport(down)),
    )
    partial = await broken.context(T0)
    assert partial.assets["errors"] == {"US10Y": "ConnectError", "VIX": "ConnectError"}
    assert partial.state == "MIXED"  # DXY alone is never a joint headwind
    await service.aclose()
    await broken.aclose()


# --- calendar --------------------------------------------------------------------

FF_ROWS = [
    {
        "title": "Non-Farm Employment Change",
        "country": "USD",
        "date": "2026-11-06T08:30:00-05:00",
        "impact": "High",
        "forecast": "65K",
        "previous": "29K",
    },
    {
        "title": "Unemployment Rate",
        "country": "USD",
        "date": "2026-11-06T08:30:00-05:00",
        "impact": "High",
        "forecast": "4.2%",
        "previous": "4.2%",
    },
    {
        "title": "ISM Services PMI",
        "country": "USD",
        "date": "2026-11-04T10:00:00-05:00",
        "impact": "Medium",
    },
    {
        "title": "ECB President Speaks",
        "country": "EUR",
        "date": "2026-11-04T09:00:00-05:00",
        "impact": "High",
    },
    {"title": "Broken", "country": "USD", "date": "soon", "impact": "High"},
]
NFP = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)


def test_forexfactory_and_manual_events():
    high = parse_forexfactory(FF_ROWS, {"USD"}, "High")
    assert [e.title for e in high] == [
        "Non-Farm Employment Change",
        "Unemployment Rate",
    ]
    assert high[0].at == NFP and high[0].forecast == "65K"
    assert len(parse_forexfactory(FF_ROWS, {"USD"}, "Medium")) == 3
    extra = parse_extra(
        "2026-12-09T14:00-05:00 FOMC statement|2026-12-09 no offset|bad"
    )
    assert [(e.title, e.at.hour) for e in extra] == [("FOMC statement", 14)]


def ff_client(rows=FF_ROWS, fail=False):
    def handler(request):
        if fail:
            return httpx.Response(503)
        return httpx.Response(200, json=rows)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_calendar_blackout_window_reminders_and_cache(tmp_path):
    settings = Settings()
    calendar = EconomicCalendar(settings, ff_client(), tmp_path / "cal.json")
    assert await calendar.refresh(NFP - timedelta(days=1))
    assert not calendar.blackout(NFP - timedelta(minutes=31))
    assert [e.title for e in calendar.blackout(NFP - timedelta(minutes=30))] == [
        "Non-Farm Employment Change",
        "Unemployment Rate",
    ]
    assert calendar.blackout(NFP + timedelta(minutes=90))
    assert not calendar.blackout(NFP + timedelta(minutes=91))
    assert calendar.due_reminders(NFP - timedelta(minutes=31)) == []
    due = calendar.due_reminders(NFP - timedelta(minutes=29))
    assert len(due) == 1 and len(due[0]) == 2  # released together: one reminder
    calendar.mark_reminded(due[0])
    assert calendar.due_reminders(NFP - timedelta(minutes=10)) == []
    # A restart keeps events and sent reminders; a failed refresh keeps the events.
    restarted = EconomicCalendar(settings, ff_client(fail=True), tmp_path / "cal.json")
    assert not await restarted.refresh(NFP - timedelta(minutes=20))
    assert restarted.last_error == "HTTPStatusError" and len(restarted.events) == 2
    assert restarted.due_reminders(NFP - timedelta(minutes=10)) == []
    assert not await restarted.refresh(NFP - timedelta(minutes=19))  # not stale yet


async def test_reminder_is_sent_once_with_forecasts(tmp_path):
    settings = Settings()
    calendar = EconomicCalendar(settings, ff_client(), tmp_path / "cal.json")
    sent = []

    async def send(text):
        sent.append(text)

    now = NFP - timedelta(minutes=25)
    assert await remind_once(calendar, settings, send, now) == 1
    assert await remind_once(calendar, settings, send, now + timedelta(minutes=1)) == 0
    text = sent[0]
    assert "ЧЕРЕЗ 30 МИН" in text and "Non-Farm Employment Change" in text
    assert "прогноз 65K · пред. 29K" in text
    assert "14:00–16:00" in text  # pause window in Europe/Brussels (CET)


def test_reminder_text_uses_local_time():
    events = parse_forexfactory(FF_ROWS[:1], {"USD"}, "High")
    text = format_reminder(events, Settings().tzinfo, 30, 30, 90)
    assert "06.11 14:30 (Europe/Brussels) · 13:30 UTC" in text


# --- macro news --------------------------------------------------------------------


def item(provider, title, source_type=SourceType.OFFICIAL_PRIMARY, symbols=(GOLD,)):
    now = datetime.now(UTC)
    return NormalizedNewsItem(
        id=f"{provider}:{title}",
        provider=provider,
        provider_item_id=title,
        source="Test",
        source_type=source_type,
        title=title,
        published_at=now,
        received_at=now,
        symbols=list(symbols),
        raw_payload_hash="h",
    )


@pytest.mark.parametrize(
    ("provider", "title", "rule", "level"),
    [
        (
            "fed_monetary",
            "Federal Reserve issues FOMC statement",
            "macro:fomc_statement",
            "CRITICAL",
        ),
        (
            "fed_speeches",
            "Bowman, Supervisory Modernization",
            "macro:default",
            "MEDIUM",
        ),
        (
            "bea_releases",
            "Personal Income and Outlays, September 2026",
            "macro:inflation_data",
            "CRITICAL",
        ),
        (
            "whitehouse_news",
            "Fact Sheet: President Imposes Tariffs on Imports",
            "macro:tariffs",
            "CRITICAL",
        ),
        (
            "whitehouse_news",
            "President Is Powering a Manufacturing Renaissance",
            "noise:not_macro_relevant",
            "LOW",
        ),
        ("ecb_press", "Monetary policy decisions", "macro:ecb_policy", "HIGH"),
    ],
)
def test_official_macro_items_classified_for_gold(provider, title, rule, level):
    news_item = item(provider, title)
    result = classify(news_item)
    matches = NewsEntityResolver(load_coverage()).resolve(news_item)
    assert result.rule == rule and matches[0].symbol == GOLD
    status = verification([news_item.source_type])
    impact = importance(
        news_item, result, matches, {GOLD: "TIER_1"}, status, Freshness.VERY_FRESH
    )
    assert impact.level == level


def test_headline_channel_items_and_crypto_rules_never_fire_on_macro():
    post = channel_item(
        "@wire",
        7,
        "FED CUTS RATES BY 25 BPS\nmore text",
        T0,
        T0,
        [GOLD],
        "https://t.me/wire/7",
    )
    assert post is not None and post.title == "FED CUTS RATES BY 25 BPS"
    assert post.summary == "more text" and post.source_type == SourceType.AGGREGATOR
    assert classify(post).event_type == EventType.MONETARY_POLICY
    assert channel_item("@wire", 8, "  \n ", T0, T0, [GOLD]) is None
    # "upgrade" is a crypto rule; on a macro feed it is just an unmatched Fed item.
    assert (
        classify(item("fed_speeches", "Upgrade of payment systems")).rule
        == "macro:default"
    )


async def test_fast_headline_then_fed_confirmation_for_gold(sqlite_database_url):
    sender = Recorder()
    pipeline = pipeline_for(
        sqlite_database_url,
        sender,
        telegram_bot_token="123:abc",
        scanner_telegram_chat_id="777",
        scanner_whitelist=GOLD,
    )
    await pipeline.start([])
    try:
        await pipeline.submit(
            item("telegram_channels", "FED CUTS RATES BY 25 BPS", SourceType.AGGREGATOR)
        )
        await until(lambda: len(sender.sent) == 1)
        first = sender.sent[0][0]
        assert first.startswith("⚠️ <b>ПЕРВИЧНОЕ СООБЩЕНИЕ</b>")
        assert "GOLD (XAU/USD)" in first and "ФРС" in first
        await pipeline.submit(
            item("fed_monetary", "Federal Reserve issues FOMC statement")
        )
        await until(lambda: len(sender.sent) == 2)
        assert sender.sent[1][0].startswith("✅ <b>НОВОСТЬ ПОДТВЕРЖДЕНА</b>")
        # Whitelist = gold only: a critical crypto headline is stored, not alerted.
        await pipeline.submit(item("wire", "$ETH bridge exploit reported", symbols=()))
        await until(
            lambda: pipeline.metrics.counters["news_urgent_not_whitelisted"] == 1
        )
    finally:
        await pipeline.stop()
    assert len(sender.sent) == 2


RSS = """<rss><channel><item><title>Federal Reserve issues FOMC statement</title>
<link>https://www.federalreserve.gov/x.htm</link><guid>x</guid>
<pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate></item></channel></rss>"""


async def test_macro_feeds_tag_gold_and_poll_fast_around_events():
    names = [p.name for p in default_providers(Settings(news_macro_enabled=True), None)]
    assert names[:1] == ["cftc_press"] and "fed_monetary" in names
    assert "telegram_channels" not in names  # needs api_id/api_hash
    near = [True]
    feed = RssProvider(
        "fed_monetary",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "Federal Reserve",
        "test",
        60,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=RSS))
        ),
        symbols=[GOLD],
        burst=lambda: near[0],
        burst_interval=10,
    )
    items = await feed.poll()
    assert items[0].symbols == [GOLD]
    assert feed.current_interval() == 10
    near[0] = False
    assert feed.current_interval() == 60
    await feed.aclose()


# --- TradingView -> bot ------------------------------------------------------------


def pine_payload(**overrides):
    """Exactly the JSON QS PRO Gold v2.3 sends (f_json)."""
    body = {
        "secret": "s" * 24,
        "event_id": "QS_XAUUSD_60_1793736000000",
        "symbol": "XAUUSD",
        "timeframe": "1h",
        "event": "SIGNAL",
        "direction": "LONG",
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": 3931.87,
        "value": 86,
        "grade": "A+",
        "score": 86,
        "stop": 3915.2,
        "tp1": 3948.54,
        "tp2": 3965.21,
        "tp3": 3981.88,
        "lots": 0.15,
        "risk_usd": 250.05,
        "reason": "HTF✓ Daily✓ Structure✓ Zone✓ PDL sweep✓ AO✓",
        "source": "QS PRO Gold v2.3",
    }
    return json.loads(json.dumps({**body, **overrides}))


def test_tradingview_signal_is_stored_and_forwarded(client, session, monkeypatch):
    from app.api.routers import tradingview
    from app.config import get_settings
    from tests.test_scanner_service import save_ready

    save_ready(session)  # an AAAUSDT analysis; gold has none yet
    sent = []

    async def fake_send(settings, text):
        sent.append(text)

    monkeypatch.setattr(tradingview, "send_forward", fake_send)
    settings = Settings(
        tradingview_webhook_enabled=True,
        tradingview_webhook_secret="s" * 24,
        telegram_bot_token="123:abc",
        scanner_telegram_chat_id="777",
        scanner_whitelist=GOLD,
        calendar_enabled=False,
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    try:
        response = client.post("/api/webhooks/tradingview", json=pine_payload())
        assert response.status_code == 200 and response.json()["forwarded"]
        assert (
            client.post(
                "/api/webhooks/tradingview", json=pine_payload(timeframe="45")
            ).status_code
            == 422
        )
    finally:
        client.app.dependency_overrides.clear()
    text = sent[0]
    assert text.startswith("📈 <b>TRADINGVIEW: A+ LONG</b> · GOLD (XAU/USD)")
    assert "SL <code>3915.2</code>" in text and "TP3 <code>3981.88</code>" in text
    assert "0.15 лот · риск $250.05" in text
    assert "Бот ещё не анализировал" in text


def test_tradingview_agreement_with_the_bot():
    from app.api.routers.tradingview import TradingViewPayload, bot_symbol
    from app.scanner.domain import Ticker
    from app.scanner.scoring import build_result
    from app.telegram.tradingview import format_tradingview_signal

    settings = Settings(scanner_whitelist=GOLD)
    assert bot_symbol("XAUUSD", settings) == GOLD
    assert bot_symbol("XAUUSDT_P", Settings()) == GOLD
    assert bot_symbol("BTCUSDT_P", settings) == "BTCUSDT"
    data = Derivatives(
        funding_state="NEUTRAL", interpretation="NEW_LONG_PARTICIPATION_POSSIBLE"
    )
    result = build_result(
        GOLD,
        aligned_frames(),
        data,
        MarketContext(
            kind="GOLD_MACRO", state="BULLISH", assets={"VIX": {"vix": 17.0}}
        ),
        Ticker(symbol=GOLD, price=112, quote_volume=1e9, timestamp=T0),
        T0,
        settings,
    )
    payload = TradingViewPayload.model_validate(pine_payload())
    text = format_tradingview_signal(payload, GOLD, result, None, settings, T0)
    assert "✅ Бот тоже видит готовый сетап" in text
    assert "фон поддерживает рост золота · VIX 17.0" in text
    from app.telegram.tradingview import agreement

    bearish_view = result.model_copy(deep=True)
    for setup in bearish_view.setups:
        setup.readiness, setup.score = (
            "WAIT_RETEST",
            (36 if setup.direction == "SHORT" else 21),
        )
    bearish_view.context.risk_off = True
    assert "но макро-фон против него" in agreement("LONG", bearish_view, settings)
    short = TradingViewPayload.model_validate(pine_payload(direction="SHORT"))
    assert "ПРОТИВОПОЛОЖНОЕ" in format_tradingview_signal(
        short, GOLD, result, None, settings, T0
    )
