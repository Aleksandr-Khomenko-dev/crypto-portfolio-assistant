"""Assemble the news pipeline for a process (runner or API). Context only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import Settings
from app.db.session import get_session_factory
from app.news.entities import load_coverage
from app.news.pipeline import NewsPipeline
from app.news.providers.base import NewsProvider
from app.news.providers.sources import default_providers
from app.services.telegram_service import TelegramService

if TYPE_CHECKING:
    from app.services.scanner_service import ScannerRuntime

# The pipeline running in this process, for /api/news/status (None if not running).
RUNNING: NewsPipeline | None = None


def build_pipeline(
    settings: Settings, runtime: ScannerRuntime
) -> tuple[NewsPipeline, list[NewsProvider]]:
    telegram = TelegramService(settings)
    chat = settings.scanner_telegram_chat_id
    sender = None
    if settings.news_send_telegram and telegram.enabled and chat:

        async def sender(text: str, reply_to: int | None) -> list[int]:
            return await telegram.send_message(chat, text, reply_to)

    pipeline = NewsPipeline(
        settings,
        get_session_factory(),
        load_coverage(),
        sender,
        mode="DRY_RUN" if settings.scanner_dry_run else "LIVE",
    )
    # Setup persisted/expired -> the in-memory active-setup index refreshes lazily.
    runtime.setup_listeners.append(pipeline.index.mark_dirty)
    # BingX notices go through the scanner's own BingX client and rate limiter.
    bingx = runtime.registry.get("BINGX")
    return pipeline, default_providers(settings, bingx)
