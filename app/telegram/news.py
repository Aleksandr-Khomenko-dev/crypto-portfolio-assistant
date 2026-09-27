"""Russian Telegram news messages (HTML). Deterministic templates only.

Provider headlines are shown verbatim (escaped); the Russian lines are rule-based
summaries of the classified event, never machine-translated or invented facts.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from app.news.domain import (
    EntityMatch,
    EventType,
    ImpactLevel,
    MatchType,
    NewsDirection,
    ProcessedNews,
    SourceType,
    Verification,
)
from app.news.repository import ActiveSetupRef, NewsContext
from app.telegram.i18n import catalog
from app.telegram.scanner import Mode

SEPARATOR = "━━━━━━━━━━━━━━━━━━"
LEVEL_ICON = {"CRITICAL": "🚨", "HIGH": "🔴", "MEDIUM": "🟠", "LOW": "⚪"}
DIRECTION = {
    NewsDirection.POTENTIALLY_ADVERSE: "🟠 Направление: потенциально негативное",
    NewsDirection.POTENTIALLY_SUPPORTIVE: "🟢 Направление: потенциально поддерживающее",
    NewsDirection.MIXED: "🟡 Направление: смешанное",
    NewsDirection.UNCLEAR: "⚪ Направление: пока неясно",
}
RELATION = {
    MatchType.DIRECT_SYMBOL: "прямая",
    MatchType.PROJECT_NAME: "прямая",
    MatchType.ORGANIZATION: "прямая (организация проекта)",
    MatchType.ECOSYSTEM: "экосистема",
    MatchType.INDIRECT: "косвенная",
}
SOURCE_KIND = {
    SourceType.OFFICIAL_PRIMARY: "официальный",
    SourceType.NEWSWIRE: "новостное агентство",
    SourceType.PUBLICATION: "издание",
    SourceType.AGGREGATOR: "агрегатор",
    SourceType.SOCIAL: "соцсети",
}
SUMMARY = {
    EventType.EXPLOIT: "Сообщается об эксплойте, затрагивающем {name}.",
    EventType.HACK: "Сообщается о взломе или инциденте безопасности, связанном с {name}.",
    EventType.CHAIN_HALT: "Сообщается об остановке сети {name}.",
    EventType.OUTAGE: "Сообщается о сбое в работе {name}.",
    EventType.DEPEG: "Сообщается о потере привязки (depeg) {name}.",
    EventType.LISTING: "Объявлен листинг: {name}.",
    EventType.DELISTING: "Объявлен делистинг: {name}.",
    EventType.WITHDRAWAL_SUSPENSION: "Приостановка депозитов/выводов: {name}.",
    EventType.NETWORK_UPGRADE: "Объявлено обновление сети {name}.",
    EventType.HARD_FORK: "Объявлен хардфорк {name}.",
    EventType.TOKEN_UNLOCK: "Разблокировка токенов {name}.",
    EventType.TOKEN_BURN: "Сжигание токенов {name}.",
    EventType.TOKENOMICS_CHANGE: "Изменение токеномики или контракта {name}.",
    EventType.ETF: "Новость об ETF, связанная с {name}.",
    EventType.REGULATION: "Регуляторная новость, затрагивающая {name}.",
    EventType.ENFORCEMENT: "Принудительные меры регулятора, связанные с {name}.",
    EventType.LAWSUIT: "Судебный иск, связанный с {name}.",
    EventType.BANKRUPTCY: "Сообщение о банкротстве, связанном с {name}.",
    EventType.GOVERNANCE: "Governance-решение или голосование {name}.",
    EventType.PARTNERSHIP: "Партнёрство с участием {name}.",
    EventType.INTEGRATION: "Интеграция с участием {name}.",
    EventType.OTHER: "Новость, связанная с {name}.",
}
SEVERE = {
    EventType.EXPLOIT,
    EventType.HACK,
    EventType.CHAIN_HALT,
    EventType.DEPEG,
    EventType.BANKRUPTCY,
    EventType.OUTAGE,
}


def ago(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds} сек назад"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


def why_it_matters(event: EventType, direction: NewsDirection, linked: bool) -> str:
    if event in SEVERE:
        return (
            "События такого типа могут резко повысить волатильность и риск; "
            "исходные условия сценария стоит перепроверить."
        )
    if event == EventType.DELISTING:
        return "Делистинг может изменить ликвидность и условия торговли контрактом."
    if event == EventType.WITHDRAWAL_SUSPENSION:
        return "Приостановка переводов может повлиять на ликвидность и спреды."
    if direction == NewsDirection.UNCLEAR:
        return (
            "Новость может повысить волатильность текущего setup."
            if linked
            else "Новость может повысить волатильность; направление влияния пока неясно."
        )
    return "Новость может повлиять на волатильность; влияние носит вероятностный, а не гарантированный характер."


def _name(news: ProcessedNews) -> str:
    symbols = [m.symbol.removesuffix("USDT") for m in news.matches[:3]]
    return ", ".join(symbols) if symbols else news.item.source


def _prefix(mode: Mode) -> list[str]:
    if mode == "LIVE":
        return []
    return [catalog("ru")[f"mode.{mode}"], ""]


def _core(news: ProcessedNews, match: EntityMatch | None, now: datetime) -> list[str]:
    event = news.classification.event_type
    lines = [
        (
            f"{LEVEL_ICON[news.importance.level]} <b>{news.importance.level.value} IMPACT</b>"
            f" · {ago(news.item.received_at, now)}"
        ),
        f"🔎 Событие: {event.value} · важность {news.importance.score}/100",
        "",
        escape(SUMMARY[event].format(name=_name(news))),
        f"<i>{escape(news.item.title)}</i>",
        "",
    ]
    if match is not None:
        lines.append(
            f"🎯 Связь: {escape(match.symbol.removesuffix('USDT'))} · {RELATION[match.match_type]}"
        )
    lines += [
        DIRECTION[news.classification.direction],
        f"🏛 Источник: {escape(news.item.source)} ({SOURCE_KIND[news.item.source_type]})",
        f"✔️ Статус проверки: {news.verification.value}",
    ]
    return lines


def _footer(news: ProcessedNews, mode: Mode) -> list[str]:
    published = news.item.published_at.strftime("%d.%m.%Y · %H:%M UTC")
    return [
        "",
        f"🕐 Опубликовано: {published}",
        catalog("ru")[f"footer.{mode}"],
        SEPARATOR,
        (
            "<i>Новости — только контекст: оценка setup не меняется.\n"
            "Образовательная информация. Автоматической торговли нет.</i>"
        ),
    ]


def format_setup_followup(
    news: ProcessedNews,
    setup: ActiveSetupRef,
    match: EntityMatch,
    now: datetime,
    mode: Mode = "LIVE",
) -> str:
    direction = "🟢 LONG" if setup.direction == "LONG" else "🔴 SHORT"
    return "\n".join(
        [
            *_prefix(mode),
            "📰 <b>НОВЫЙ КОНТЕКСТ ДЛЯ АКТИВНОГО СИГНАЛА</b>",
            SEPARATOR,
            f"🪙 <b>{escape(setup.symbol)}</b> · {direction}",
            f"⭐ Текущий setup: <b>{setup.score} / 100</b> (не меняется из-за новости)",
            "",
            *_core(news, match, now),
            "",
            "💡 <b>Почему это важно:</b>",
            why_it_matters(
                news.classification.event_type, news.classification.direction, True
            ),
            *_footer(news, mode),
        ]
    )


def format_urgent(
    news: ProcessedNews,
    match: EntityMatch | None,
    affected: list[ActiveSetupRef],
    now: datetime,
    kind: str,
    mode: Mode = "LIVE",
) -> str:
    """kind: URGENT (confirmed critical), URGENT_PRELIMINARY or URGENT_CONFIRMED."""
    title = {
        "URGENT": "🚨 <b>СРОЧНАЯ НОВОСТЬ</b>",
        "URGENT_PRELIMINARY": "⚠️ <b>ПЕРВИЧНОЕ СООБЩЕНИЕ</b>",
        "URGENT_CONFIRMED": "✅ <b>НОВОСТЬ ПОДТВЕРЖДЕНА</b>",
    }[kind]
    lines = [*_prefix(mode), title, SEPARATOR]
    if match is not None:
        lines.append(f"🪙 <b>{escape(match.symbol.removesuffix('USDT'))}</b>")
    lines += _core(news, match, now)
    if kind == "URGENT_PRELIMINARY":
        lines += [
            "",
            "<i>Пока один источник; подтверждение будет отправлено отдельно.</i>",
        ]
    if affected:
        lines += ["", "📌 <b>Затронутые setup сканера:</b>"]
        lines += [
            f"• {s.direction} {escape(s.symbol)} {s.score}/100 (сценарий НЕ отменяется автоматически)"
            for s in affected[:5]
        ]
    lines += [
        "",
        "💡 <b>Почему это важно:</b>",
        why_it_matters(
            news.classification.event_type,
            news.classification.direction,
            bool(affected),
        ),
        *_footer(news, mode),
    ]
    return "\n".join(lines)


def format_news_block(contexts: list[NewsContext], now: datetime) -> list[str]:
    """Compact block appended to a scanner setup alert (max 3 events)."""
    if not contexts:
        return []
    lines = ["📰 <b>НОВОСТНОЙ КОНТЕКСТ</b>"]
    for context in contexts[:3]:
        relation = RELATION.get(MatchType(context.match_type), "косвенная")
        level = ImpactLevel(context.level)
        lines += [
            (
                f"{LEVEL_ICON[level]} {level.value} · {ago(context.received_at, now)} · "
                f"{escape(context.event_type)}"
            ),
            f"<i>{escape(context.title[:160])}</i>",
            (
                f"🎯 Связь: {escape(context.matched_symbol.removesuffix('USDT'))} — "
                f"{relation} · 🏛 {escape(context.source)} · "
                f"{escape(Verification(context.verification).value)}"
            ),
        ]
    lines.append("<i>Новости — контекст, на оценку setup не влияют.</i>")
    return lines
