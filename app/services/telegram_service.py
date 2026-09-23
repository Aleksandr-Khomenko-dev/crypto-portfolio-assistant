from __future__ import annotations

from aiogram import Bot

from app.config import Settings, get_settings
from app.db.models import DailyDigest, Portfolio, Signal
from app.telegram.formatters import format_digest_message, format_signal_messages


class TelegramService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    async def send_signals(self, portfolio: Portfolio, signals: list[Signal]) -> bool:
        if not self.enabled or not portfolio.telegram_chat_id or not signals:
            return False
        text = format_signal_messages(portfolio.name, signals)
        return await self._send_text(portfolio.telegram_chat_id, text)

    async def send_digest(self, portfolio: Portfolio, digest: DailyDigest) -> bool:
        if not self.enabled or not portfolio.telegram_chat_id:
            return False
        return await self._send_text(
            portfolio.telegram_chat_id, format_digest_message(digest.content)
        )

    async def send_scanner(self, chat_id: str, text: str) -> bool:
        if not self.enabled:
            return False
        return await self._send_text(chat_id, text)

    async def send_test(self, chat_id: str, text: str) -> list[int]:
        """Connectivity check: returns Telegram message ids (empty if disabled)."""
        if not self.enabled:
            return []
        return await self._deliver(chat_id, text)

    async def _send_text(self, chat_id: str, text: str) -> bool:
        return bool(await self._deliver(chat_id, text))

    async def _deliver(self, chat_id: str, text: str) -> list[int]:
        from aiogram.enums import ParseMode

        token = self.settings.telegram_bot_token
        if not token:
            raise RuntimeError("CPDA_TELEGRAM_BOT_TOKEN is not configured")
        bot = Bot(token=token)
        try:
            # Split into chunks ≤4096 chars (Telegram limit) preserving line boundaries
            chunks = _split_message(text, limit=4096)
            ids = []
            for chunk in chunks:
                message = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )
                ids.append(message.message_id)
            return ids
        finally:
            await bot.session.close()


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split a long message into chunks without breaking lines."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks
