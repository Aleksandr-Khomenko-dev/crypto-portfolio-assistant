from __future__ import annotations

from decimal import Decimal

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.config import get_settings
from app.db.session import get_session_factory
from app.providers.factory import create_market_provider
from app.providers.interfaces import AssetMarketQuery
from app.services.asset_service import AssetService
from app.services.monitoring_service import MonitoringService
from app.services.pnl_service import compute_realized_pnl
from app.services.portfolio_service import PortfolioService
from app.services.signal_service import SignalService
from app.telegram.formatters import format_signal_explanation

router = Router(name="portfolio_signal_agent")

_SEVERITY_ICON = {"low": "🟡", "medium": "🟠", "high": "🔴"}
_SIGNAL_ICON = {
    "abnormal_rise": "📈",
    "abnormal_drop": "📉",
    "extreme_pump": "🚀",
    "take_profit": "💰",
    "pullback": "↩️",
    "risk": "⚠️",
    "morning_digest": "☀️",
}


def _match_portfolio(service: PortfolioService, query: str | None):
    portfolios = service.list_portfolios()
    if not portfolios:
        return None
    if not query:
        return portfolios[0]
    normalized = query.strip().lower()
    for p in portfolios:
        if p.name.lower() == normalized or str(p.id) == normalized:
            return p
    return None


def _fc(v: float | Decimal) -> str:
    v = float(v)
    sign = "+" if v >= 0 else ""
    return f"{sign}${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _fp(v: float | Decimal | None) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    return f"{v:+.2f}%"


async def _reply(message: Message, text: str) -> None:
    await message.answer(text, parse_mode=ParseMode.HTML)


# ── /start ─────────────────────────────────────────────────────────────────


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await _reply(
        message,
        "🤖 <b>Portfolio Signal Agent</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Режим: только мониторинг · без автоторговли.\n\n"
        "Быстрые команды:\n"
        "  /pnl · /price BTC · /top · /worst\n"
        "  /signals · /alerts · /digest\n\n"
        "Напиши /help для полного списка.",
    )


# ── /help ──────────────────────────────────────────────────────────────────


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await _reply(
        message,
        "📋 <b>Команды бота</b>\n"
        "Scanner: /scanner · /toplong · /topshort · /setup SYMBOL · /watchlist\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💼 <b>/pnl</b> — P&amp;L сводка портфеля\n"
        "💲 <b>/price</b> &lt;МОНЕТА&gt; — текущая цена\n"
        "🏆 <b>/top</b> — топ-5 по росту за 24ч\n"
        "📉 <b>/worst</b> — топ-5 по падению за 24ч\n"
        "🔔 <b>/signals</b> — последние сигналы\n"
        "🎯 <b>/alerts</b> — цели TP/SL\n"
        "☀️ <b>/digest</b> — утренний дайджест\n"
        "📊 <b>/portfolio</b> — обзор позиций\n"
        "⚠️ <b>/risk</b> — оценка рисков\n"
        "🔍 <b>/explain</b> &lt;id&gt; — детали сигнала\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>К любой команде можно добавить имя портфеля.\n"
        "Без имени — первый портфель.</i>",
    )


# ── /pnl ───────────────────────────────────────────────────────────────────


@router.message(Command("pnl"))
async def pnl_handler(message: Message, command: CommandObject) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return

        total_cost = sum(Decimal(str(p.cost_basis)) for p in portfolio.positions)
        realized = compute_realized_pnl(session, portfolio.id)

    from app.api.routers.dashboard import _get_latest_snapshots

    with session_factory() as session:
        asset_ids = [p.asset_id for p in portfolio.positions]
        snaps = _get_latest_snapshots(session, asset_ids)
        pos_map = {p.asset_id: p for p in portfolio.positions}
        total_value = Decimal(0)
        for aid, snap in snaps.items():
            pos = pos_map.get(aid)
            if pos and snap.price_usd:
                total_value += Decimal(str(pos.quantity)) * Decimal(str(snap.price_usd))

    upnl = total_value - total_cost
    upnl_pct = (upnl / total_cost * 100) if total_cost else Decimal(0)
    net_real = realized.net_realized_usd

    pnl_icon = "🟢" if upnl >= 0 else "🔴"
    real_icon = "🟢" if net_real >= 0 else "🔴"

    lines = [
        f"💼 <b>{portfolio.name}</b> — P&amp;L",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📥 Инвестировано:    <code>{_fc(total_cost)}</code>",
        f"💰 Тек. оценка:      <code>{_fc(total_value)}</code>",
        f"{pnl_icon} Нереализ. P&amp;L:   <code>{_fc(upnl)}</code>  (<code>{_fp(upnl_pct)}</code>)",
        f"{real_icon} Реализ. P&amp;L:    <code>{_fc(net_real)}</code>",
    ]
    if realized.sell_count:
        lines.append(f"📤 Закрыто сделок:   <code>{realized.sell_count}</code>")
    if realized.per_asset:
        best = max(realized.per_asset.items(), key=lambda x: x[1])
        worst = min(realized.per_asset.items(), key=lambda x: x[1])
        lines.append(f"🏆 Лучшая фиксация: <code>{best[0]} {_fc(best[1])}</code>")
        if worst != best:
            lines.append(
                f"💔 Худшая фиксация:  <code>{worst[0]} {_fc(worst[1])}</code>"
            )

    await _reply(message, "\n".join(lines))


# ── /price ─────────────────────────────────────────────────────────────────


@router.message(Command("price", "asset"))
async def price_handler(message: Message, command: CommandObject) -> None:
    ticker = (command.args or "").strip().upper()
    if not ticker:
        await _reply(message, "ℹ️ Использование: <code>/price ETH</code>")
        return

    settings = get_settings()
    session_factory = get_session_factory()
    with session_factory() as session:
        asset = AssetService(session).get_by_symbol(ticker)
        if asset is None:
            await _reply(message, f"❌ Монета <b>{ticker}</b> не найдена в базе.")
            return
        # Find position for context
        from sqlalchemy import select

        from app.db.models import Position

        pos = session.scalars(
            select(Position).where(Position.asset_id == asset.id).limit(1)
        ).first()
        avg_entry = Decimal(str(pos.average_entry_price)) if pos else None
        qty = Decimal(str(pos.quantity)) if pos else None

    provider = create_market_provider(settings)
    try:
        quote = await provider.fetch_quote(
            AssetMarketQuery(
                asset_id=asset.id,
                symbol=asset.symbol,
                name=asset.name,
                coingecko_id=asset.coingecko_id,
                binance_symbol=asset.binance_symbol,
                bybit_symbol=asset.bybit_symbol,
            )
        )
    finally:
        await provider.aclose()

    if quote is None:
        await _reply(message, f"⚠️ Нет данных по <b>{ticker}</b> прямо сейчас.")
        return

    price = quote.price_usd
    h1 = quote.change_1h_pct
    h24 = quote.change_24h_pct
    h1_icon = ("📈" if h1 and h1 >= 0 else "📉") if h1 is not None else ""
    h24_icon = ("📈" if h24 and h24 >= 0 else "📉") if h24 is not None else ""

    lines = [
        f"💲 <b>{ticker}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Цена:   <code>${price:,.6f}</code>",
        f"{h1_icon} 1ч:     <code>{_fp(h1)}</code>",
        f"{h24_icon} 24ч:    <code>{_fp(h24)}</code>",
    ]
    if avg_entry and avg_entry > 0:
        change_from_entry = (price - avg_entry) / avg_entry * 100
        icon = "🟢" if change_from_entry >= 0 else "🔴"
        lines.append(
            f"{icon} vs вход: <code>{_fp(change_from_entry)}</code>  (avg <code>${avg_entry:,.4f}</code>)"
        )
    if qty:
        val = qty * price
        lines.append(f"💼 Оценка позиции: <code>${val:,.2f}</code>")
    lines.append(f"\n<i>Источник: {quote.provider}</i>")

    await _reply(message, "\n".join(lines))


# ── /alerts ────────────────────────────────────────────────────────────────


@router.message(Command("alerts"))
async def alerts_handler(message: Message, command: CommandObject) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return

        positions_with_alerts = [
            p
            for p in portfolio.positions
            if p.price_alert_take_profit_usd is not None
            or p.price_alert_stop_loss_usd is not None
        ]

    if not positions_with_alerts:
        await _reply(
            message,
            f"💼 <b>{portfolio.name}</b>\n\n"
            "📭 Нет активных целей TP/SL.\n"
            "<i>Задай цели через кнопку 🎯 в дашборде.</i>",
        )
        return

    lines = [f"🎯 <b>{portfolio.name}</b> — цели TP/SL", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for pos in positions_with_alerts:
        sym = pos.asset.symbol
        tp = pos.price_alert_take_profit_usd
        sl = pos.price_alert_stop_loss_usd
        parts = []
        if tp:
            parts.append(f"TP <code>${float(tp):,.4f}</code>")
        if sl:
            parts.append(f"SL <code>${float(sl):,.4f}</code>")
        lines.append(f"  • <b>{sym}</b>: {' | '.join(parts)}")

    await _reply(message, "\n".join(lines))


# ── /signals ───────────────────────────────────────────────────────────────


@router.message(Command("signals"))
async def signals_handler(message: Message, command: CommandObject) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return
        signals = SignalService(session).list_recent_signals(portfolio.id, limit=7)

    if not signals:
        await _reply(message, f"💼 <b>{portfolio.name}</b>\n\n😌 Нет свежих сигналов.")
        return

    lines = [
        f"🔔 <b>{portfolio.name}</b> — последние сигналы",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for s in signals:
        sev = _SEVERITY_ICON.get(s.severity.value, "⚪")
        typ = _SIGNAL_ICON.get(s.signal_type.value, "📊")
        ts = s.created_at.strftime("%d %b %H:%M")
        lines.append(f"{sev}{typ} <b>{s.title}</b>")
        lines.append(f"   <i>{s.action_idea}</i>  <code>{ts}</code>")

    await _reply(message, "\n".join(lines))


# ── /portfolio ─────────────────────────────────────────────────────────────


@router.message(Command("portfolio"))
async def portfolio_handler(message: Message, command: CommandObject) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return

        total_cost = sum(Decimal(str(p.cost_basis)) for p in portfolio.positions)

    from app.api.routers.dashboard import _get_latest_snapshots

    with session_factory() as session:
        asset_ids = [p.asset_id for p in portfolio.positions]
        snaps = _get_latest_snapshots(session, asset_ids)

        top_positions = []
        for pos in portfolio.positions:
            snap = snaps.get(pos.asset_id)
            cur_price = (
                Decimal(str(snap.price_usd)) if snap and snap.price_usd else Decimal(0)
            )
            val = (
                Decimal(str(pos.quantity)) * cur_price
                if cur_price
                else Decimal(str(pos.cost_basis))
            )
            pnl = val - Decimal(str(pos.cost_basis))
            pnl_pct = (
                (pnl / Decimal(str(pos.cost_basis)) * 100)
                if pos.cost_basis
                else Decimal(0)
            )
            top_positions.append((pos.asset.symbol, float(val), float(pnl_pct)))

    top_positions.sort(key=lambda x: x[1], reverse=True)

    lines = [
        f"📊 <b>{portfolio.name}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Позиций: <code>{len(portfolio.positions)}</code>   Инвестировано: <code>{_fc(total_cost)}</code>",
        "",
        "<b>Топ позиций (по оценке):</b>",
    ]
    for sym, display_value, display_pnl_pct in top_positions[:8]:
        icon = "🟢" if display_pnl_pct >= 0 else "🔴"
        lines.append(
            f"  {icon} <b>{sym}</b>  <code>${display_value:,.2f}</code>  <code>{display_pnl_pct:+.1f}%</code>"
        )

    await _reply(message, "\n".join(lines))


# ── /risk ──────────────────────────────────────────────────────────────────


@router.message(Command("risk"))
async def risk_handler(message: Message, command: CommandObject) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return
        provider = create_market_provider(settings)
        try:
            metrics = await MonitoringService(
                session, provider, settings=settings
            ).preview_portfolio(portfolio)
        finally:
            await provider.aclose()

    status_icon = {"CALM": "😌", "WATCH": "👀", "RISK": "🚨", "OPPORTUNITY": "🎯"}.get(
        metrics.action_summary.split(" — ")[0], "📊"
    )
    lines = [
        f"⚠️ <b>{portfolio.name}</b> — риски",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{status_icon} <b>{metrics.action_summary}</b>",
    ]
    if metrics.concentration_warnings:
        lines.append("\n🎯 <b>Концентрация:</b>")
        lines.extend([f"  ❗ {w}" for w in metrics.concentration_warnings[:4]])
    else:
        lines.append("\n✅ Концентрации в норме.")

    await _reply(message, "\n".join(lines))


# ── /digest ────────────────────────────────────────────────────────────────


@router.message(Command("digest"))
async def digest_handler(message: Message, command: CommandObject) -> None:
    settings = get_settings()
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return
        provider = create_market_provider(settings)
        try:
            result = await MonitoringService(
                session, provider, settings=settings
            ).create_morning_digest(portfolio)
        finally:
            await provider.aclose()

    from app.telegram.formatters import format_digest_message

    await _reply(message, format_digest_message(result.digest.content))


# ── /top and /worst ────────────────────────────────────────────────────────


@router.message(Command("top"))
async def top_handler(message: Message, command: CommandObject) -> None:
    """Show top 5 positions by 24h % change."""
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return

    from app.api.routers.dashboard import _get_latest_snapshots

    with session_factory() as session:
        asset_ids = [p.asset_id for p in portfolio.positions]
        snaps = _get_latest_snapshots(session, asset_ids)

    rows = []
    for pos in portfolio.positions:
        snap = snaps.get(pos.asset_id)
        h24 = (
            float(snap.change_24h_pct)
            if snap and snap.change_24h_pct is not None
            else None
        )
        cur = Decimal(str(snap.price_usd)) if snap and snap.price_usd else None
        val = Decimal(str(pos.quantity)) * cur if cur else Decimal(str(pos.cost_basis))
        rows.append((pos.asset.symbol, h24, float(val)))

    ranked = sorted(
        [r for r in rows if r[1] is not None], key=lambda x: x[1], reverse=True
    )

    lines = [f"🏆 <b>{portfolio.name}</b> — Топ за 24ч", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for i, (sym, h24, display_value) in enumerate(ranked[:5], 1):
        lines.append(
            f"  {i}. 📈 <b>{sym}</b>  <code>{h24:+.2f}%</code>  <code>${display_value:,.2f}</code>"
        )
    if not ranked:
        lines.append("😶 Нет данных по 24ч")

    await _reply(message, "\n".join(lines))


@router.message(Command("worst"))
async def worst_handler(message: Message, command: CommandObject) -> None:
    """Show bottom 5 positions by 24h % change."""
    session_factory = get_session_factory()
    with session_factory() as session:
        portfolio = _match_portfolio(PortfolioService(session), command.args)
        if portfolio is None:
            await _reply(message, "❌ Портфель не найден.")
            return

    from app.api.routers.dashboard import _get_latest_snapshots

    with session_factory() as session:
        asset_ids = [p.asset_id for p in portfolio.positions]
        snaps = _get_latest_snapshots(session, asset_ids)

    rows = []
    for pos in portfolio.positions:
        snap = snaps.get(pos.asset_id)
        h24 = (
            float(snap.change_24h_pct)
            if snap and snap.change_24h_pct is not None
            else None
        )
        cur = Decimal(str(snap.price_usd)) if snap and snap.price_usd else None
        val = Decimal(str(pos.quantity)) * cur if cur else Decimal(str(pos.cost_basis))
        rows.append((pos.asset.symbol, h24, float(val)))

    ranked = sorted([r for r in rows if r[1] is not None], key=lambda x: x[1])

    lines = [f"📉 <b>{portfolio.name}</b> — Аутсайдеры 24ч", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    for i, (sym, h24, display_value) in enumerate(ranked[:5], 1):
        lines.append(
            f"  {i}. 📉 <b>{sym}</b>  <code>{h24:+.2f}%</code>  <code>${display_value:,.2f}</code>"
        )
    if not ranked:
        lines.append("😶 Нет данных по 24ч")

    await _reply(message, "\n".join(lines))


# ── /explain ───────────────────────────────────────────────────────────────


@router.message(Command("explain"))
async def explain_handler(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await _reply(
            message, "ℹ️ Использование: <code>/explain &lt;signal_id&gt;</code>"
        )
        return
    from uuid import UUID

    try:
        signal_id = UUID(raw)
    except ValueError:
        await _reply(message, "❌ Неверный формат ID.")
        return

    session_factory = get_session_factory()
    with session_factory() as session:
        signal = SignalService(session).get_signal(signal_id)
        if signal is None:
            await _reply(message, "❌ Сигнал не найден.")
            return
        await _reply(message, format_signal_explanation(signal))
