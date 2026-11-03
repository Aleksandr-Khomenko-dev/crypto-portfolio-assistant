"""One-time login for the Telegram headline-channel news source (interactive).

    python -m scripts.telegram_news_login

Asks for your phone number, the login code Telegram sends you and, if enabled, your
2FA password. It then checks every channel in CPDA_NEWS_TELEGRAM_CHANNELS. The
session file it writes is a login credential for your account: never share or commit
it (*.session is git-ignored).
"""

from __future__ import annotations

import asyncio

from app.config import PROJECT_ROOT, get_settings
from app.news.providers.telegram_channels import channel_list


async def main() -> None:
    settings = get_settings()
    if not (settings.news_telegram_api_id and settings.news_telegram_api_hash):
        raise SystemExit(
            "Set CPDA_NEWS_TELEGRAM_API_ID and CPDA_NEWS_TELEGRAM_API_HASH in .env "
            "(https://my.telegram.org -> API development tools)."
        )
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise SystemExit("Install the client first: pip install telethon") from exc
    client = TelegramClient(
        str(PROJECT_ROOT / settings.news_telegram_session),
        settings.news_telegram_api_id,
        settings.news_telegram_api_hash,
    )
    await client.start()
    me = await client.get_me()
    print("Logged in as", getattr(me, "username", None) or me.id)
    for channel in channel_list(settings.news_telegram_channels):
        try:
            entity = await client.get_entity(channel)
            print("OK  ", channel, "-", getattr(entity, "title", ""))
        except Exception as exc:  # noqa: BLE001 - report every channel
            print("FAIL", channel, "-", type(exc).__name__)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
