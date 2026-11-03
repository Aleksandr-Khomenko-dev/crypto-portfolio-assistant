"""Push news source: public Telegram headline channels, read through YOUR account.

Headline channels relay wire headlines seconds after they cross, usually faster than
any free website or RSS feed. They are aggregators, not primary sources: one headline
can produce at most an URGENT_PRELIMINARY alert, upgraded to CONFIRMED when an official
feed (e.g. the Fed RSS) reports the same event type for the same market.

Setup (once):
1. https://my.telegram.org -> API development tools -> api_id and api_hash.
2. .env: CPDA_NEWS_TELEGRAM_API_ID, CPDA_NEWS_TELEGRAM_API_HASH and
   CPDA_NEWS_TELEGRAM_CHANNELS=@channel1,@channel2 (channels you trust).
3. pip install telethon; python -m scripts.telegram_news_login (phone + code).
The session file (<project>/<session>.session) is a login credential: keep it private.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.config import PROJECT_ROOT, Settings
from app.news.domain import NormalizedNewsItem, SourceType, Transport
from app.news.providers.base import NewsProvider, Submit, payload_hash

logger = logging.getLogger(__name__)
NAME = "telegram_channels"


def channel_list(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def channel_item(
    channel: str,
    message_id: int,
    text: str,
    published_at: datetime,
    received_at: datetime,
    symbols: list[str],
    url: str | None = None,
) -> NormalizedNewsItem | None:
    """First non-empty line = headline; the rest is the summary."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None
    item_id = f"{channel.lstrip('@').lower()}:{message_id}"
    return NormalizedNewsItem(
        id=f"{NAME}:{item_id}",
        provider=NAME,
        provider_item_id=item_id,
        source=f"Telegram {channel}",
        source_type=SourceType.AGGREGATOR,
        title=lines[0][:300],
        summary=" ".join(lines[1:])[:1000],
        url=url,
        published_at=published_at.astimezone(UTC),
        received_at=received_at,
        symbols=list(symbols),
        raw_payload_hash=payload_hash(
            {"channel": channel, "id": message_id, "text": text}
        ),
    )


class TelegramChannelsProvider(NewsProvider):
    name = NAME
    transport = Transport.STREAM

    def __init__(self, settings: Settings, symbols: list[str]) -> None:
        # Channels can be silent for hours; Telethon reconnects on its own.
        super().__init__(stale_after_seconds=86400, interval=None)
        self.settings, self.symbols = settings, symbols
        self.channels = channel_list(settings.news_telegram_channels)

    async def run(self, submit: Submit, stop: asyncio.Event) -> None:
        if not self.channels:
            logger.warning("Telegram news: CPDA_NEWS_TELEGRAM_CHANNELS is empty")
            return
        try:
            from telethon import (  # type: ignore[import-not-found]
                TelegramClient,
                events,
            )
        except ImportError:
            logger.error("Telegram news: `pip install telethon` to enable this source")
            self.health.errors += 1
            return
        client = TelegramClient(
            str(PROJECT_ROOT / self.settings.news_telegram_session),
            self.settings.news_telegram_api_id,
            self.settings.news_telegram_api_hash,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                logger.error(
                    "Telegram news: not logged in; run python -m scripts.telegram_news_login"
                )
                self.health.errors += 1
                return

            @client.on(events.NewMessage(chats=self.channels))
            async def on_message(event) -> None:
                received_at = datetime.now(UTC)  # stamp before any parsing work
                self.health.success()
                message = event.message
                chat = await event.get_chat()
                username = getattr(chat, "username", None)
                item = channel_item(
                    "@" + username if username else str(event.chat_id),
                    message.id,
                    message.message or "",
                    message.date,
                    received_at,
                    self.symbols,
                    f"https://t.me/{username}/{message.id}" if username else None,
                )
                if item is not None:
                    await self._emit(submit, [item])

            self.health.success()
            logger.info("Telegram news listening channels=%s", self.channels)
            disconnected = asyncio.ensure_future(client.run_until_disconnected())
            stopper = asyncio.create_task(stop.wait())
            await asyncio.wait(
                {disconnected, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            stopper.cancel()
        finally:
            await client.disconnect()
