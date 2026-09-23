from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import StrategyProfile, StrategyProfileCode, UserSettings


DEFAULT_STRATEGY_PROFILES = (
    {
        "code": StrategyProfileCode.MAIN,
        "name": "Main Portfolio",
        "description": "More active spot portfolio with staged take-profit guidance.",
        "rule_overrides": {
            "take_profit_style": "active",
            "alert_frequency": "normal",
        },
    },
    {
        "code": StrategyProfileCode.LONG_TERM,
        "name": "Long-Term Kids Portfolio",
        "description": "Conservative long-term portfolio with lower alert frequency.",
        "rule_overrides": {
            "take_profit_style": "conservative",
            "alert_frequency": "low",
        },
    },
)


def ensure_reference_data(session: Session) -> None:
    for profile_data in DEFAULT_STRATEGY_PROFILES:
        existing = session.scalar(
            select(StrategyProfile).where(StrategyProfile.code == profile_data["code"])
        )
        if existing is None:
            session.add(StrategyProfile(**profile_data))

    default_settings = session.scalar(
        select(UserSettings).where(UserSettings.user_key == "default")
    )
    if default_settings is None:
        session.add(
            UserSettings(
                user_key="default",
                timezone="Europe/Brussels",
                default_quote_currency="USD",
                morning_digest_hour=8,
                morning_digest_minute=0,
                alerts_enabled=True,
            )
        )

    session.commit()
