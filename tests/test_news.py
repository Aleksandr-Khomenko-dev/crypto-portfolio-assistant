"""News context: pure rules (matching, noise, classification, importance, clustering,
formatting). No I/O."""

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
import respx

from app.news.classify import classify, freshness, importance, verification
from app.news.cluster import Clusterer
from app.news.domain import (
    EventType,
    Freshness,
    ImpactLevel,
    MatchType,
    NewsDirection,
    NormalizedNewsItem,
    ProcessedNews,
    SourceType,
    Verification,
)
from app.news.entities import (
    DEFAULT_CONFIG,
    Coverage,
    NewsEntityResolver,
    load_coverage,
)
from app.news.providers.sources import title_symbols

NOW = datetime(2026, 9, 24, 13, 2, 11, tzinfo=UTC)
COVERAGE = load_coverage()
RESOLVER = NewsEntityResolver(COVERAGE)


def item(
    title,
    *,
    source_type=SourceType.OFFICIAL_PRIMARY,
    provider="test",
    age_min=1,
    symbols=(),
    hint=None,
    url=None,
    item_id=None,
    summary="",
):
    return NormalizedNewsItem(
        id=f"{provider}:{item_id or title}",
        provider=provider,
        provider_item_id=item_id or title,
        source="Test",
        source_type=source_type,
        title=title,
        summary=summary,
        url=url,
        published_at=NOW - timedelta(minutes=age_min),
        received_at=NOW,
        symbols=list(symbols),
        event_type_hint=hint,
        raw_payload_hash="h",
    )


def matches(title, **kwargs):
    return {(m.symbol, m.match_type) for m in RESOLVER.resolve(item(title, **kwargs))}


# --- entity resolution ----------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("ARB unlock schedule published", ("ARBUSDT", MatchType.DIRECT_SYMBOL)),
        ("Arbitrum One sequencer update", ("ARBUSDT", MatchType.PROJECT_NAME)),
        ("Arbitrum Foundation treasury vote", ("ARBUSDT", MatchType.PROJECT_NAME)),
        ("Offchain Labs ships a new client", ("ARBUSDT", MatchType.ORGANIZATION)),
        ("OP Mainnet upgrade scheduled", ("OPUSDT", MatchType.PROJECT_NAME)),
        ("Optimism Foundation grants", ("OPUSDT", MatchType.PROJECT_NAME)),
        ("New chains join the Superchain", ("OPUSDT", MatchType.ECOSYSTEM)),
        ("Ethereum Foundation research update", ("ETHUSDT", MatchType.PROJECT_NAME)),
        ("Vitalik Buterin proposes new EIP", ("ETHUSDT", MatchType.INDIRECT)),
        ("BTC hashrate record", ("BTCUSDT", MatchType.DIRECT_SYMBOL)),
    ],
)
def test_direct_symbols_aliases_and_organizations(title, expected):
    assert expected in matches(title)


@pytest.mark.parametrize(
    "title",
    [
        "The fee will be near zero, one analyst says",  # NEAR, ONE are ordinary words
        "Dot plot suggests a link between rates and gas prices",
        "Traders expect op-ed coverage to time the top",
        "SEC charges firm over $100 million fraud (SEC)",  # $100 / (SEC) are not tickers
    ],
)
def test_ambiguous_words_are_never_mapped_blindly(title):
    assert matches(title) == set()


def test_ambiguous_ticker_needs_explicit_form():
    assert ("OPUSDT", MatchType.DIRECT_SYMBOL) in matches("$OP rallies after vote")
    assert ("OPUSDT", MatchType.DIRECT_SYMBOL) in matches("Optimism (OP) upgrade")
    assert ("OPUSDT", MatchType.DIRECT_SYMBOL) in matches("BingX adds OPUSDT perpetual")


def test_official_notice_titles_yield_asset_tags():
    assert title_symbols(
        "BingX Will Delist TAC From Spot Trading & Other Services"
    ) == ["TACUSDT"]
    assert title_symbols("BingX Adds DAL, HONA  for Perpetual Futures") == [
        "DALUSDT",
        "HONAUSDT",
    ]
    assert title_symbols("BingX Delists KIOXIAUSDT for Perpetual Futures") == [
        "KIOXIAUSDT"
    ]


def test_portfolio_priority_comes_from_data_not_code():
    coverage = Coverage(projects=COVERAGE.projects, ambiguous=COVERAGE.ambiguous)
    assert coverage.level("ARBUSDT") == "TIER_2"
    assert coverage.level("MICROUSDT") == "UNCOVERED"
    coverage.portfolio = frozenset({"MICROUSDT", "ARBUSDT"})
    assert coverage.level("MICROUSDT") == "PORTFOLIO"
    assert coverage.level("ARBUSDT") == "PORTFOLIO"
    source = DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert "quantity" not in source and "position" not in source.lower()


# --- noise and classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("ARB price prediction: could reach $5", "price_prediction"),
        ("Top 10 coins to buy this week", "listicle"),
        ("Join our trading competition and win a bonus", "promotion"),
        ("Analysts say ETH may rebound", "analyst_chatter"),
    ],
)
def test_noise_is_filtered(title, reason):
    result = classify(item(title))
    assert result.noise and result.noise_reason == reason


def test_regulator_items_need_crypto_context():
    assert classify(
        item("SEC Charges Investment Adviser With Fraud", provider="sec_press")
    ).noise
    crypto = classify(item("SEC Charges Crypto Firm With Fraud", provider="sec_press"))
    assert not crypto.noise and crypto.event_type == EventType.ENFORCEMENT


@pytest.mark.parametrize(
    ("title", "event", "direction"),
    [
        (
            "Arbitrum One exploit drains $40M",
            EventType.EXPLOIT,
            NewsDirection.POTENTIALLY_ADVERSE,
        ),
        (
            "Network halted after consensus bug",
            EventType.CHAIN_HALT,
            NewsDirection.POTENTIALLY_ADVERSE,
        ),
        (
            "Stablecoin lost its peg overnight",
            EventType.DEPEG,
            NewsDirection.POTENTIALLY_ADVERSE,
        ),
        (
            "SEC approves spot Ether ETF",
            EventType.ETF,
            NewsDirection.POTENTIALLY_SUPPORTIVE,
        ),
        ("Exchange will list ARB perpetuals", EventType.LISTING, NewsDirection.UNCLEAR),
        ("Ethereum hard fork date set", EventType.HARD_FORK, NewsDirection.UNCLEAR),
        ("Token unlock of 5% supply", EventType.TOKEN_UNLOCK, NewsDirection.UNCLEAR),
        ("Something happened today", EventType.OTHER, NewsDirection.UNCLEAR),
    ],
)
def test_event_types_and_conservative_direction(title, event, direction):
    result = classify(item(title))
    assert (result.event_type, result.direction) == (event, direction)


def test_provider_category_is_authoritative():
    result = classify(item("BingX notice", hint="Delisting"))
    assert result.event_type == EventType.DELISTING and result.rule == "bingx_delisting"


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (3, Freshness.VERY_FRESH),
        (40, Freshness.FRESH),
        (150, Freshness.RECENT),
        (600, Freshness.HISTORICAL),
    ],
)
def test_freshness_windows(minutes, expected):
    assert freshness(NOW - timedelta(minutes=minutes), NOW) == expected


def test_verification_never_claims_primary_without_primary_source():
    assert verification([SourceType.NEWSWIRE]) == Verification.SINGLE_SOURCE
    assert (
        verification([SourceType.NEWSWIRE, SourceType.PUBLICATION])
        == Verification.MULTI_SOURCE_CONFIRMED
    )
    assert (
        verification([SourceType.SOCIAL, SourceType.SOCIAL]) == Verification.UNVERIFIED
    )
    assert (
        verification([SourceType.NEWSWIRE, SourceType.OFFICIAL_PRIMARY])
        == Verification.PRIMARY_CONFIRMED
    )


def impact(title, **kwargs):
    news_item = item(title, **kwargs)
    found = RESOLVER.resolve(news_item)
    result = classify(news_item)
    return importance(
        news_item,
        result,
        found,
        {m.symbol: COVERAGE.level(m.symbol) for m in found},
        verification([news_item.source_type]),
        freshness(news_item.published_at, NOW),
    )


def test_impact_levels_are_gated_by_severity():
    assert impact("Arbitrum One exploit drains $40M").level == ImpactLevel.CRITICAL
    halted = impact("Arbitrum One chain halted", source_type=SourceType.NEWSWIRE)
    assert halted.level == ImpactLevel.HIGH  # single newswire: preliminary
    assert impact("Arbitrum One chain halted").level == ImpactLevel.CRITICAL
    routine = impact("BingX to Update the Maintenance Margin Rate Rules for ZILUSDT")
    assert routine.level == ImpactLevel.LOW  # excellent sourcing, trivial event
    assert impact("Arbitrum Foundation partners with a studio").level == ImpactLevel.LOW
    assert impact("ARB price prediction").score == 0
    old = impact("Arbitrum One exploit drains $40M", age_min=600)
    assert old.score < impact("Arbitrum One exploit drains $40M").score


# --- clustering -----------------------------------------------------------------


def test_cross_source_clustering_prefers_official_canonical():
    clusters = Clusterer()
    wire, new = clusters.assign(
        "wire:1",
        "https://wire/1",
        SourceType.NEWSWIRE,
        EventType.CHAIN_HALT,
        {"ARBUSDT"},
        NOW,
    )
    assert new and wire.verification == Verification.SINGLE_SOURCE
    official, new = clusters.assign(
        "official:1",
        "https://arbitrum/1",
        SourceType.OFFICIAL_PRIMARY,
        EventType.CHAIN_HALT,
        {"ARBUSDT"},
        NOW + timedelta(minutes=4),
    )
    assert not new and official is wire
    assert official.canonical_url == "https://arbitrum/1"
    assert official.verification == Verification.PRIMARY_CONFIRMED
    assert official.item_ids == ["wire:1", "official:1"]  # every source kept
    other, new = clusters.assign(
        "wire:2", None, SourceType.NEWSWIRE, EventType.LISTING, {"ARBUSDT"}, NOW
    )
    assert new and other is not wire  # different event type -> different event


def test_clusters_expire_after_window():
    clusters = Clusterer()
    first, _ = clusters.assign(
        "a", None, SourceType.NEWSWIRE, EventType.HACK, {"ETHUSDT"}, NOW
    )
    clusters.prune(NOW + timedelta(hours=7))
    second, new = clusters.assign(
        "b",
        None,
        SourceType.NEWSWIRE,
        EventType.HACK,
        {"ETHUSDT"},
        NOW + timedelta(hours=7),
    )
    assert new and second is not first


# --- Telegram formatting --------------------------------------------------------


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ("b", "i", "code"):
            self.bad.append(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append("/" + tag)


def valid(text):
    parser = Markup()
    parser.feed(text)
    return not parser.bad and not parser.stack


def processed(title, **kwargs):
    news_item = item(title, **kwargs)
    found = RESOLVER.resolve(news_item)
    result = classify(news_item)
    status = verification([news_item.source_type])
    return ProcessedNews(
        item=news_item,
        matches=found,
        classification=result,
        importance=importance(
            news_item,
            result,
            found,
            {m.symbol: COVERAGE.level(m.symbol) for m in found},
            status,
            freshness(news_item.published_at, NOW),
        ),
        verification=status,
        freshness=freshness(news_item.published_at, NOW),
        coverage={m.symbol: COVERAGE.level(m.symbol) for m in found},
    )


def setup_ref(score=84):
    import uuid

    from app.news.repository import ActiveSetupRef

    return ActiveSetupRef(
        setup_id=uuid.uuid4(),
        exchange="BINGX",
        symbol="ARBUSDT",
        direction="LONG",
        score=score,
        state="HIGH_CONFLUENCE",
        readiness="WAIT_RETEST",
        created_at=NOW,
        telegram_message_id=77,
    )


def test_russian_followup_keeps_score_and_is_valid_html():
    from app.telegram.news import format_setup_followup

    news = processed("Arbitrum Foundation announces <b>treasury</b> vote")
    text = format_setup_followup(
        news, setup_ref(84), news.matches[0], NOW + timedelta(seconds=2)
    )
    assert text.startswith("📰 <b>НОВЫЙ КОНТЕКСТ ДЛЯ АКТИВНОГО СИГНАЛА</b>")
    assert "⭐ Текущий setup: <b>84 / 100</b> (не меняется из-за новости)" in text
    assert "🎯 Связь: ARB · прямая" in text and "⚪ Направление: пока неясно" in text
    assert "Governance-решение или голосование ARB." in text  # deterministic Russian
    assert "&lt;b&gt;treasury&lt;/b&gt;" in text and valid(text)
    for banned in ("buy", "sell", "enter now", "guaranteed", "high probability"):
        assert banned not in text.lower()


def test_urgent_preliminary_and_confirmed_wording():
    from app.telegram.news import format_urgent

    wire = processed("Arbitrum One chain halted", source_type=SourceType.NEWSWIRE)
    text = format_urgent(
        wire, wire.matches[0], [setup_ref()], NOW, "URGENT_PRELIMINARY"
    )
    assert text.startswith("⚠️ <b>ПЕРВИЧНОЕ СООБЩЕНИЕ</b>")
    assert "✔️ Статус проверки: SINGLE_SOURCE" in text
    assert "LONG ARBUSDT 84/100 (сценарий НЕ отменяется автоматически)" in text
    confirmed = format_urgent(wire, wire.matches[0], [], NOW, "URGENT_CONFIRMED")
    assert confirmed.startswith("✅ <b>НОВОСТЬ ПОДТВЕРЖДЕНА</b>") and valid(confirmed)
    dry = format_urgent(wire, wire.matches[0], [], NOW, "URGENT", mode="DRY_RUN")
    assert dry.startswith("🧪 <b>DRY RUN</b>")


# --- live-source parsing (payload shapes captured 2026-09-24) -------------------


async def test_bingx_announcements_parse_and_share_the_scanner_client():
    from unittest.mock import AsyncMock

    from app.news.providers.sources import BingXAnnouncements

    row = {
        "contentType": "Delisting",
        "content": "<div><strong>Dear Users,</strong></div><br/>BingX will delist TAC.",
        "releaseTime": "2026-09-24T16:45:31.000+08:00",
        "title": "BingX Will Delist TAC From Spot Trading & Other Services",
        "url": "https://bingx.com/en-us/support/articles/17740275837455",
    }
    bingx = AsyncMock()
    bingx.announcements = AsyncMock(
        side_effect=lambda c: [row] if c == "Delisting" else []
    )
    items = await BingXAnnouncements(bingx, 30).poll()
    assert [c.args[0] for c in bingx.announcements.call_args_list] == list(
        BingXAnnouncements.CATEGORIES
    )
    [item] = items
    assert item.published_at == datetime(2026, 9, 24, 8, 45, 31, tzinfo=UTC)
    assert item.symbols == ["TACUSDT"] and item.event_type_hint == "Delisting"
    assert item.summary.startswith("Dear Users,") and "<" not in item.summary
    assert item.source_type == SourceType.OFFICIAL_PRIMARY


RSS = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title>CFTC Charges Crypto Platform With Fraud</title>
<link>https://www.cftc.gov/PressRoom/PressReleases/9302-26</link>
<guid>https://www.cftc.gov/PressRoom/PressReleases/9302-26</guid>
<pubDate>Tue, 22 Sep 2026 20:38:00 +0000</pubDate><description></description></item>
</channel></rss>"""


@respx.mock
async def test_rss_conditional_get_and_parsing():
    import httpx

    from app.news.providers.sources import RssProvider

    url = "https://www.cftc.gov/RSS/RSSGP/rssgp.xml"
    route = respx.get(url).mock(
        side_effect=[
            httpx.Response(200, content=RSS, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )
    provider = RssProvider("cftc_press", url, "U.S. CFTC", "CPDA test agent", 60)
    try:
        [item] = await provider.poll()
        assert item.published_at == datetime(2026, 9, 22, 20, 38, tzinfo=UTC)
        assert classify(item).event_type == EventType.ENFORCEMENT
        assert await provider.poll() == []
        assert route.calls[1].request.headers["If-None-Match"] == '"v1"'
        assert route.calls[0].request.headers["User-Agent"] == "CPDA test agent"
    finally:
        await provider.aclose()


def test_sec_feed_disabled_without_a_contact_user_agent():
    from unittest.mock import MagicMock

    from app.config import Settings
    from app.news.providers.sources import default_providers

    names = [p.name for p in default_providers(Settings(), MagicMock())]
    assert names == ["bingx_announcements", "cftc_press"]
    configured = Settings(news_sec_user_agent="Research contact example@example.com")
    assert "sec_press" in [p.name for p in default_providers(configured, MagicMock())]


def test_build_pipeline_wires_index_refresh_and_shared_bingx_client():
    from app.config import Settings
    from app.news.service import build_pipeline
    from app.providers.bingx_futures import BingXFuturesProvider
    from app.services.scanner_service import ScannerRuntime

    settings = Settings()
    runtime = ScannerRuntime(BingXFuturesProvider(settings), settings)
    pipeline, providers = build_pipeline(settings, runtime)
    assert providers[0].bingx is runtime.registry.get("BINGX")  # one shared limiter
    assert pipeline.sender is None  # no Telegram configured in tests
    runtime.setups_changed()
    assert pipeline.index._dirty
