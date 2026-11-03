"""Background loop: keep the calendar fresh and send event reminders (runner only)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.config import Settings
from app.macro.calendar import EconomicCalendar
from app.telegram.calendar import format_reminder

logger = logging.getLogger(__name__)
Send = Callable[[str], Awaitable[object]]


async def remind_once(
    calendar: EconomicCalendar, settings: Settings, send: Send | None, now: datetime
) -> int:
    await calendar.refresh(now)
    sent = 0
    for group in calendar.due_reminders(now):
        text = format_reminder(
            group,
            settings.tzinfo,
            settings.calendar_reminder_minutes,
            settings.calendar_pre_minutes,
            settings.calendar_post_minutes,
        )
        if send is not None:
            try:
                await send(text)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - one failed send is not retried
                logger.warning("Calendar reminder failed error=%s", type(exc).__name__)
        # Marked even without Telegram or after a failure: no reminder storm later.
        calendar.mark_reminded(group)
        logger.info("Calendar reminder %s", [e.title for e in group])
    return sent


async def calendar_forever(
    stop: asyncio.Event,
    calendar: EconomicCalendar,
    settings: Settings,
    send: Send | None,
    every: float = 30,
) -> None:
    while not stop.is_set():
        try:
            await remind_once(calendar, settings, send, datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 - never stops the runner
            logger.warning("Calendar loop error=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except TimeoutError:
            pass
