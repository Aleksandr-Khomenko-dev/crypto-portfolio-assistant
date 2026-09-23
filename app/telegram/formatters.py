from __future__ import annotations

from decimal import Decimal

from app.db.models import (
    Signal,
    SignalSeverity,
    SignalType,
)

# ── Emoji helpers ────────────────────────────────────────────────────────────

_SEVERITY_EMOJI: dict[str, str] = {
    SignalSeverity.LOW.value: "🟡",
    SignalSeverity.MEDIUM.value: "🟠",
    SignalSeverity.HIGH.value: "🔴",
}

_TYPE_EMOJI: dict[str, str] = {
    SignalType.ABNORMAL_RISE.value: "📈",
    SignalType.ABNORMAL_DROP.value: "📉",
    SignalType.EXTREME_PUMP.value: "🚀",
    SignalType.TAKE_PROFIT.value: "💰",
    SignalType.PULLBACK.value: "↩️",
    SignalType.RISK.value: "⚠️",
    SignalType.MORNING_DIGEST.value: "☀️",
}


def _sev(signal: Signal) -> str:
    return _SEVERITY_EMOJI.get(signal.severity.value, "⚪")


def _typ(signal: Signal) -> str:
    return _TYPE_EMOJI.get(signal.signal_type.value, "📊")


def _pct(value: Decimal | float | None, plus: bool = True) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    sign = "+" if plus and v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _usd(value: Decimal | float | None) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    sign = "+" if v >= 0 else ""
    return f"{sign}${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _mini_bar(pct: float, width: int = 8) -> str:
    """Visual bar for percentage 0–100."""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


# ── Signal formatting ─────────────────────────────────────────────────────────


def format_signal_messages(portfolio_name: str, signals: list[Signal]) -> str:
    """Format one or more signals as a Telegram-friendly message with emojis."""
    count = len(signals)
    header = (
        f"🔔 <b>{portfolio_name}</b>\n"
        f"{'━' * 28}\n"
        f"<b>{count} {'сигнал' if count == 1 else 'сигнала' if count < 5 else 'сигналов'}</b>\n"
    )

    blocks: list[str] = [header]
    for s in signals:
        sev_emoji = _sev(s)
        typ_emoji = _typ(s)
        block = (
            f"{sev_emoji} {typ_emoji} <b>{s.title}</b>\n"
            f"💡 <i>{s.action_idea}</i>\n"
            f"📌 {s.reasoning}\n"
            f"⚡ Риск: {s.risk_note}"
        )
        blocks.append(block)

    blocks.append("\n⏰ Мониторинг активен · обновление каждые 15 мин")
    return ("\n" + "─" * 28 + "\n").join(blocks)


def format_signal_explanation(signal: Signal) -> str:
    sev_emoji = _sev(signal)
    typ_emoji = _typ(signal)
    conf = float(signal.confidence_score) * 100 if signal.confidence_score else 0
    conf_bar = _mini_bar(conf)

    return (
        f"{sev_emoji} {typ_emoji} <b>{signal.title}</b>\n"
        f"{'━' * 28}\n"
        f"💡 <b>Действие:</b> {signal.action_idea}\n"
        f"📌 <b>Причина:</b> {signal.reasoning}\n"
        f"⚡ <b>Риск:</b> {signal.risk_note}\n"
        f"🎯 <b>Уверенность:</b> [{conf_bar}] {conf:.0f}%\n\n"
        f"{signal.explanation}"
    )


# ── Digest formatting ─────────────────────────────────────────────────────────

_STATUS_CONFIG: dict[str, tuple[str, str]] = {
    "CALM": ("😌", "Спокойно"),
    "WATCH": ("👀", "Наблюдение"),
    "RISK": ("🚨", "Требует внимания"),
    "OPPORTUNITY": ("🎯", "Возможность"),
}


def format_digest_message(content: str) -> str:
    """
    Re-format the plain-text digest content into an emoji-rich Telegram message.
    The content is produced by DigestService.build_digest_text() and already
    contains structured sections — we parse and re-render it here.
    """
    lines = [l.rstrip() for l in content.splitlines()]

    def find_section(keyword: str) -> list[str]:
        items: list[str] = []
        in_section = False
        for line in lines:
            if line.strip().lower().startswith(keyword.lower()):
                in_section = True
                continue
            if in_section:
                if line.strip() == "" or (line and not line.startswith(" ")):
                    break
                items.append(line.strip())
        return items

    # Extract key values from the digest text
    portfolio_name = (
        lines[0].split(" — ")[0].removesuffix(" morning digest")
        if lines
        else "Portfolio"
    )
    status_raw = ""
    value_str = ""
    pnl_str = ""
    profile_str = ""
    change_str = ""

    for line in lines[:10]:
        if line.startswith("Status:"):
            status_raw = line.replace("Status:", "").strip().strip("[]")
        elif line.startswith("Value:"):
            parts = line.replace("Value:", "").strip().split("|")
            value_str = parts[0].strip()
            change_str = parts[1].strip() if len(parts) > 1 else ""
        elif line.startswith("P&L:"):
            pnl_str = line.replace("P&L:", "").strip()
        elif line.startswith("Profile:"):
            profile_str = line.replace("Profile:", "").strip()

    # Determine pnl direction for emoji
    pnl_is_positive = pnl_str.startswith("$") and not pnl_str.startswith("$-")
    pnl_emoji = "🟢" if pnl_is_positive else "🔴"

    # 24h direction
    ch24_positive = "+" in change_str
    ch24_emoji = "📈" if ch24_positive else "📉"

    status_icon, status_label = _STATUS_CONFIG.get(status_raw, ("📊", status_raw))

    gainers = find_section("Top gainers")
    losers = find_section("Top losers")
    near_tp = find_section("Near take-profit")
    pullback = find_section("Pullback watch")
    concentration = find_section("Concentration warnings")
    recent_sigs = find_section("Recent signals")
    summary_lines = [l for l in lines if l.startswith("Summary:")]
    summary = summary_lines[0].replace("Summary:", "").strip() if summary_lines else ""

    msg_lines: list[str] = [
        f"☀️ <b>Утренний дайджест · {portfolio_name}</b>",
        f"{'━' * 30}",
        f"{status_icon} Статус: <b>{status_label}</b>",
        f"📊 Стратегия: <i>{profile_str}</i>",
        "",
        "💼 <b>Портфель</b>",
        f"  💰 Оценка:  <b>{value_str}</b>",
        f"  {ch24_emoji} 24ч:     <b>{change_str}</b>",
        f"  {pnl_emoji} P&L:     <b>{pnl_str}</b>",
    ]

    if gainers and gainers != ["No gainers yet"]:
        msg_lines.append("")
        msg_lines.append("🚀 <b>Лучшие за 24ч</b>")
        for g in gainers[:3]:
            msg_lines.append(f"  📈 {g}")

    if losers and losers != ["No losers yet"]:
        msg_lines.append("")
        msg_lines.append("📉 <b>Худшие за 24ч</b>")
        for l in losers[:3]:
            msg_lines.append(f"  🔻 {l}")

    if near_tp:
        msg_lines.append("")
        msg_lines.append("💰 <b>Зона тейк-профита</b>")
        for item in near_tp:
            msg_lines.append(f"  ✅ {item}")

    if pullback:
        msg_lines.append("")
        msg_lines.append("↩️ <b>Коррекция от максимума</b>")
        for item in pullback:
            msg_lines.append(f"  ⚠️ {item}")

    if concentration:
        msg_lines.append("")
        msg_lines.append("🎯 <b>Концентрация риска</b>")
        for item in concentration:
            msg_lines.append(f"  ❗ {item}")

    if recent_sigs and recent_sigs != ["No fresh signals."]:
        msg_lines.append("")
        msg_lines.append("🔔 <b>Активные сигналы</b>")
        for sig in recent_sigs[:5]:
            # Add severity emoji based on keyword in signal text
            if "[HIGH]" in sig:
                prefix = "🔴"
            elif "[MEDIUM]" in sig:
                prefix = "🟠"
            else:
                prefix = "🟡"
            msg_lines.append(
                f"  {prefix} {sig.replace('[HIGH]', '').replace('[MEDIUM]', '').replace('[LOW]', '').strip()}"
            )

    if summary:
        msg_lines.append("")
        msg_lines.append(f"📋 <b>Вывод:</b> <i>{summary}</i>")

    msg_lines.append(f"{'━' * 30}")
    msg_lines.append("⏰ Следующий дайджест завтра в 08:00")

    return "\n".join(msg_lines)
