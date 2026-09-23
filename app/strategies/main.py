from __future__ import annotations

from app.config import Settings
from app.db.models import StrategyProfileCode
from app.strategies.base import BaseStrategy, StrategyRuleSet


class MainStrategy(BaseStrategy):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            StrategyRuleSet(
                profile_code=StrategyProfileCode.MAIN,
                concentration_alert_pct=settings.main_concentration_alert_pct,
                drawdown_alert_pct=settings.main_drawdown_alert_pct,
                take_profit_bands=settings.main_strategy_take_profit_plan,
                take_profit_requires_extension=False,
            ),
        )
