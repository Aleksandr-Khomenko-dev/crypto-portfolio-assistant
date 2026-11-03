"""Free official sources (verified live 2026-09-24). None supports push delivery, so
they are polled at a short, fair-use cadence; upstream latency is bounded by it."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape

import httpx

from app.news.domain import NormalizedNewsItem, SourceType, Transport
from app.news.providers.base import NewsProvider, PollingProvider, payload_hash

TAG = re.compile(r"<[^>]+>")
# Upper-case tokens in exchange notice titles that are not asset names.
NOT_ASSETS = frozenset(
    {"BINGX", "USDT", "USDC", "USD", "API", "VIP", "UTC", "APR", "FAQ", "KYC", "P2P"}
)


def plain(html: str, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", unescape(TAG.sub(" ", html or ""))).strip()[:limit]


def title_symbols(title: str) -> list[str]:
    """Assets named in an official exchange notice title, as canonical pairs."""
    pairs = re.findall(r"\b([A-Z0-9]{2,15})-?USDT\b", title)
    tokens = [
        t
        for t in re.findall(r"\b[A-Z][A-Z0-9]{1,14}\b", title)
        if t not in NOT_ASSETS and not t.endswith("USDT")  # pairs handled above
    ]
    return sorted({t + "USDT" for t in pairs + tokens if re.search(r"[A-Z]", t)})


class BingXAnnouncements(PollingProvider):
    """GET /openApi/content/v1/announcement (public). Uses the scanner's BingX client,
    so polling shares its rate limiter with scanning and OI collection."""

    name = "bingx_announcements"
    transport = Transport.REST_POLL
    CATEGORIES = (
        "Delisting",
        "FuturesListing",
        "AssetMaintenance",
        "SystemMaintenance",
    )
    ASSET_CATEGORIES = frozenset({"Delisting", "FuturesListing", "SpotListing"})

    def __init__(self, bingx, interval: float) -> None:  # bingx: BingXFuturesProvider
        super().__init__(interval)
        self.bingx = bingx

    async def poll(self) -> list[NormalizedNewsItem]:
        return [item async for batch in self.poll_batches() for item in batch]

    async def poll_batches(self) -> AsyncIterator[list[NormalizedNewsItem]]:
        # Each category is emitted as soon as it arrives: a delisting notice never
        # waits for the maintenance category request that follows it.
        for category in self.CATEGORIES:
            yield self._parse(category, await self.bingx.announcements(category))

    def _parse(self, category: str, rows: list[dict]) -> list[NormalizedNewsItem]:
        items = []
        received_at = datetime.now(UTC)
        for row in rows:
            url = row.get("url") or ""
            title = str(row.get("title") or "").strip()
            if not title or not row.get("releaseTime"):
                continue
            # BingX items carry no id; the canonical URL identifies the notice.
            item_id = sha256((url or title).encode()).hexdigest()[:32]
            items.append(
                NormalizedNewsItem(
                    id=f"{self.name}:{item_id}",
                    provider=self.name,
                    provider_item_id=item_id,
                    source="BingX",
                    source_type=SourceType.OFFICIAL_PRIMARY,
                    title=title,
                    summary=plain(str(row.get("content") or "")),
                    url=url or None,
                    published_at=datetime.fromisoformat(row["releaseTime"]),
                    received_at=received_at,
                    symbols=title_symbols(title)
                    if category in self.ASSET_CATEGORIES
                    else [],
                    event_type_hint=category,
                    raw_payload_hash=payload_hash(row),
                )
            )
        return items


class RssProvider(PollingProvider):
    """RSS 2.0 poller with conditional GET (ETag/Last-Modified) and a size cap."""

    transport = Transport.RSS_POLL
    MAX_BYTES = 2_000_000

    def __init__(
        self,
        name: str,
        url: str,
        source: str,
        user_agent: str,
        interval: float,
        client: httpx.AsyncClient | None = None,
        symbols: list[str] | None = None,
        burst: Callable[[], bool] | None = None,
        burst_interval: float | None = None,
    ) -> None:
        self.name = name
        super().__init__(interval)
        self.url, self.source = url, source
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        # BEA answers 406 to a bare "application/rss+xml"; fall back to any XML.
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }
        self.etag: str | None = None
        self.modified: str | None = None
        # Macro feeds tag every item with the markets they move (e.g. gold).
        self.symbols = list(symbols or [])
        self.burst, self.burst_interval = burst, burst_interval

    def current_interval(self) -> float:
        if self.burst is not None and self.burst_interval and self.burst():
            return self.burst_interval
        return self.interval

    async def poll(self) -> list[NormalizedNewsItem]:
        headers = dict(self.headers)
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.modified:
            headers["If-Modified-Since"] = self.modified
        response = await self.client.get(self.url, headers=headers)
        if response.status_code == 304:
            return []
        response.raise_for_status()
        if len(response.content) > self.MAX_BYTES:
            raise ValueError("RSS feed exceeds size cap")
        self.etag = response.headers.get("ETag")
        self.modified = response.headers.get("Last-Modified")
        received_at = datetime.now(UTC)
        root = ET.fromstring(response.content)
        items = []
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip() or None
            guid = (node.findtext("guid") or link or title).strip()
            published = node.findtext("pubDate")
            if not title or not published:
                continue
            item_id = sha256(guid.encode()).hexdigest()[:32]
            items.append(
                NormalizedNewsItem(
                    id=f"{self.name}:{item_id}",
                    provider=self.name,
                    provider_item_id=item_id,
                    source=self.source,
                    source_type=SourceType.OFFICIAL_PRIMARY,
                    title=title,
                    summary=plain(node.findtext("description") or ""),
                    url=link,
                    published_at=parsedate_to_datetime(published).astimezone(UTC),
                    received_at=received_at,
                    symbols=list(self.symbols),
                    raw_payload_hash=payload_hash(
                        {"guid": guid, "title": title, "published": published}
                    ),
                )
            )
        return items

    async def aclose(self) -> None:
        await self.client.aclose()


# Official gold-relevant feeds (verified live 2026-11-03). U.S. Treasury press
# releases (404) and BLS (403 for automated clients) have no usable feed.
MACRO_FEEDS = (
    (
        "fed_monetary",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "Federal Reserve",
        True,
    ),
    (
        "fed_speeches",
        "https://www.federalreserve.gov/feeds/speeches.xml",
        "Federal Reserve",
        False,
    ),
    (
        "fed_testimony",
        "https://www.federalreserve.gov/feeds/testimony.xml",
        "Federal Reserve",
        False,
    ),
    ("bea_releases", "https://apps.bea.gov/rss/rss.xml", "U.S. BEA", False),
    ("ecb_press", "https://www.ecb.europa.eu/rss/press.html", "ECB", False),
    ("whitehouse_news", "https://www.whitehouse.gov/news/feed/", "White House", False),
)
EVENT_BURST = timedelta(minutes=10)


def event_burst(calendar) -> Callable[[], bool]:
    """True within 10 minutes of a calendar event (e.g. the FOMC statement)."""

    def active() -> bool:
        now = datetime.now(UTC)
        return any(abs(event.at - now) <= EVENT_BURST for event in calendar.events)

    return active


def macro_providers(settings, calendar=None) -> list[NewsProvider]:
    symbols = sorted(
        {s.strip().upper() for s in settings.news_macro_symbols.split(",") if s.strip()}
    )
    agent = (
        settings.news_sec_user_agent or "CPDA macro news context (read-only RSS reader)"
    )
    providers: list[NewsProvider] = []
    for name, url, source, bursts in MACRO_FEEDS:
        providers.append(
            RssProvider(
                name,
                url,
                source,
                agent,
                settings.news_poll_rss_seconds,
                symbols=symbols,
                burst=event_burst(calendar)
                if bursts and calendar is not None
                else None,
                burst_interval=settings.news_event_poll_seconds,
            )
        )
    if settings.news_telegram_api_id and settings.news_telegram_api_hash:
        from app.news.providers.telegram_channels import TelegramChannelsProvider

        providers.append(TelegramChannelsProvider(settings, symbols))
    return providers


def default_providers(settings, bingx, calendar=None) -> list[NewsProvider]:
    providers: list[NewsProvider] = []
    if bingx is not None:
        providers.append(BingXAnnouncements(bingx, settings.news_poll_bingx_seconds))
    if settings.news_sec_user_agent:
        providers.append(
            RssProvider(
                "sec_press",
                "https://www.sec.gov/news/pressreleases.rss",
                "U.S. SEC",
                settings.news_sec_user_agent,
                settings.news_poll_rss_seconds,
            )
        )
    providers.append(
        RssProvider(
            "cftc_press",
            "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
            "U.S. CFTC",
            settings.news_sec_user_agent or "CPDA news context (read-only RSS reader)",
            settings.news_poll_rss_seconds,
        )
    )
    if settings.news_macro_enabled:
        providers += macro_providers(settings, calendar)
    return providers
