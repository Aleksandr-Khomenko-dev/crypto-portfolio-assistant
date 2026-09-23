from app.config import Settings
from app.db.models import StrategyProfileCode
from app.strategies.base import BaseStrategy
from app.strategies.long_term import LongTermStrategy
from app.strategies.main import MainStrategy


def get_strategy(profile_code: StrategyProfileCode, settings: Settings) -> BaseStrategy:
    if profile_code == StrategyProfileCode.LONG_TERM:
        return LongTermStrategy(settings)
    return MainStrategy(settings)


__all__ = ["BaseStrategy", "LongTermStrategy", "MainStrategy", "get_strategy"]
