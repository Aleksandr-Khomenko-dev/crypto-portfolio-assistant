from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher

from app.config import get_settings
from app.telegram.handlers import router


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    from app.telegram.scanner_handlers import router as scanner_router

    dispatcher.include_router(router)
    dispatcher.include_router(scanner_router)
    return dispatcher


async def run_bot() -> None:
    settings = get_settings()
    if not settings.telegram_polling_enabled:
        return
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to run the Telegram bot.")

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = create_dispatcher()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
