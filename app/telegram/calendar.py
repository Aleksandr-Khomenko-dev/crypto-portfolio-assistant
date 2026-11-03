"""Russian Telegram message: reminder before high-impact economic events."""

from __future__ import annotations

from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.macro.calendar import EconomicEvent

SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def format_reminder(
    group: list[EconomicEvent],
    timezone: ZoneInfo,
    lead_minutes: int,
    pre_minutes: int,
    post_minutes: int,
) -> str:
    at = group[0].at
    local = at.astimezone(timezone)
    pause_from = (at - timedelta(minutes=pre_minutes)).astimezone(timezone)
    pause_to = (at + timedelta(minutes=post_minutes)).astimezone(timezone)
    rows = []
    for event in group:
        facts = " · ".join(
            part
            for part in (
                f"прогноз {escape(event.forecast)}" if event.forecast else "",
                f"пред. {escape(event.previous)}" if event.previous else "",
            )
            if part
        )
        rows.append(
            f"• <b>{escape(event.title)}</b>" + (f" — {facts}" if facts else "")
        )
    source = ", ".join(dict.fromkeys(e.source for e in group))
    return "\n".join(
        [
            f"📅 <b>ЧЕРЕЗ {lead_minutes} МИН: ВАЖНЫЕ ДАННЫЕ {escape(group[0].currency)}</b>",
            SEPARATOR,
            f"🕐 {local:%d.%m %H:%M} ({escape(str(timezone))}) · {at.astimezone(ZoneInfo('UTC')):%H:%M} UTC",
            *rows,
            "",
            f"⏸ Новые сигналы по золоту на паузе: {pause_from:%H:%M}–{pause_to:%H:%M}",
            (
                "💡 В первые минуты после выхода цифр спред расширяется, а цена часто "
                "делает ложное движение в обе стороны. Стопы рядом с ценой выбивает первыми."
            ),
            SEPARATOR,
            (
                f"<i>Календарь: {escape(source)}. Образовательная информация. "
                "Автоматической торговли нет.</i>"
            ),
        ]
    )
