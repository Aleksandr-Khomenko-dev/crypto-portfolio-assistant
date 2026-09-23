from __future__ import annotations

from decimal import Decimal

from app.db.models import Portfolio, SignalSeverity, SignalType
from app.services.types import PortfolioMetrics, SignalCandidate

_PRICE_ALERT_COOLDOWN_MINUTES = 24 * 60


def build_price_level_alert_candidates(portfolio: Portfolio, metrics: PortfolioMetrics) -> list[SignalCandidate]:
    """Emit signals when current price crosses user-set take-profit or stop-loss USD levels."""
    by_id = {p.id: p for p in portfolio.positions}
    out: list[SignalCandidate] = []

    for pm in metrics.positions:
        pos = by_id.get(pm.position_id)
        if pos is None:
            continue

        tp = pos.price_alert_take_profit_usd
        sl = pos.price_alert_stop_loss_usd
        if tp is None and sl is None:
            continue

        price = pm.current_price
        sym = pm.symbol

        if tp is not None:
            target = Decimal(str(tp))
            if price >= target:
                out.append(
                    SignalCandidate(
                        signal_type=SignalType.TAKE_PROFIT,
                        severity=SignalSeverity.HIGH,
                        confidence_score=Decimal("0.88"),
                        title=f"🎯 {sym}: цель take-profit ${target:,.2f}",
                        message=f"Текущая цена ~${price:,.4f} достигла или выше цели ${target:,.2f}.",
                        action_idea="Рассмотри фиксацию части позиции по своему плану.",
                        reasoning="Сработала ручная цель take-profit, заданная для позиции.",
                        risk_note="Цена может уйти выше — решай по риск-профилю и таймфрейму.",
                        explanation="Уровень задан в настройках позиции (price_alert_take_profit_usd).",
                        event_key=f"price_tp:{pos.id}:{target}",
                        cooldown_minutes=_PRICE_ALERT_COOLDOWN_MINUTES,
                        asset_id=pm.asset_id,
                        position_id=pos.id,
                        metrics_json={"target_usd": str(target), "price_usd": str(price)},
                    )
                )

        if sl is not None:
            floor_p = Decimal(str(sl))
            if price <= floor_p:
                out.append(
                    SignalCandidate(
                        signal_type=SignalType.RISK,
                        severity=SignalSeverity.HIGH,
                        confidence_score=Decimal("0.88"),
                        title=f"🛑 {sym}: зона stop-loss ${floor_p:,.2f}",
                        message=f"Текущая цена ~${price:,.4f} на уровне или ниже стопа ${floor_p:,.2f}.",
                        action_idea="Проверь план: стоп, усреднение или выход — без автоторговли.",
                        reasoning="Сработала ручная отметка stop-loss для позиции.",
                        risk_note="Ложные пробои возможны; не действуй импульсивно.",
                        explanation="Уровень задан в настройках позиции (price_alert_stop_loss_usd).",
                        event_key=f"price_sl:{pos.id}:{floor_p}",
                        cooldown_minutes=_PRICE_ALERT_COOLDOWN_MINUTES,
                        asset_id=pm.asset_id,
                        position_id=pos.id,
                        metrics_json={"floor_usd": str(floor_p), "price_usd": str(price)},
                    )
                )

    return out
