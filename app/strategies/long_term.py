from __future__ import annotations

from app.config import Settings
from app.db.models import StrategyProfileCode
from app.strategies.base import BaseStrategy, StrategyRuleSet


class LongTermStrategy(BaseStrategy):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            StrategyRuleSet(
                profile_code=StrategyProfileCode.LONG_TERM,
                concentration_alert_pct=settings.long_term_concentration_alert_pct,
                drawdown_alert_pct=settings.long_term_drawdown_alert_pct,
                take_profit_bands=settings.long_term_strategy_take_profit_plan,
                take_profit_requires_extension=True,
            ),
        )
