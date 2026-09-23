"""Send exactly one Telegram connectivity message through TelegramService.

Usage: python -m app.telegram.smoke_test

Uses CPDA_TELEGRAM_BOT_TOKEN and CPDA_SCANNER_TELEGRAM_CHAT_ID from the environment or
.env. Runs no scanner or trading logic, touches no database and never prints the token.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import Settings, get_settings
from app.services.telegram_service import TelegramService

MESSAGE = (
    "🧪 Portfolio Signal Agent\n"
    "Telegram delivery test successful.\n\n"
    "Mode: connectivity test\n"
    "No trade signal was generated."
)


def missing_settings(settings: Settings) -> list[str]:
    missing = []
    if not settings.telegram_bot_token:
        missing.append("CPDA_TELEGRAM_BOT_TOKEN")
    if not settings.scanner_telegram_chat_id:
        missing.append("CPDA_SCANNER_TELEGRAM_CHAT_ID")
    return missing


def safe_error(exc: BaseException) -> str:
    # Telegram API descriptions ("chat not found") never contain the token; other
    # exception texts may embed request URLs, so only their type is shown.
    from aiogram.exceptions import TelegramAPIError

    if isinstance(exc, TelegramAPIError):
        return f"{type(exc).__name__}: {exc.message}"
    return type(exc).__name__


async def run(settings: Settings) -> int:
    missing = missing_settings(settings)
    if missing:
        print(
            "Telegram delivery NOT attempted. Missing settings: " + ", ".join(missing)
        )
        return 2
    assert settings.scanner_telegram_chat_id is not None
    try:
        ids = await asyncio.wait_for(
            TelegramService(settings).send_test(
                settings.scanner_telegram_chat_id, MESSAGE
            ),
            timeout=settings.http_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - report safely, never the token
        print("Telegram delivery FAILED: " + safe_error(exc))
        return 1
    if not ids:
        print("Telegram delivery FAILED: no message was accepted")
        return 1
    print(f"Telegram delivery succeeded. message_id={ids[0]}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run(get_settings())))


if __name__ == "__main__":
    main()
