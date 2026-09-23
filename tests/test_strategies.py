from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.config import Settings
from app.db.models import Portfolio, StrategyProfileCode
from app.providers.interfaces import MarketQuote
from app.services.types import PortfolioMetrics, PositionMetrics
from app.strategies import get_strategy


def _metrics(pnl_pct: str, move_24h_pct: str, weight_pct: str) -> PortfolioMetrics:
    quote = MarketQuote(
        asset_id=uuid4(),
        provider="test",
        symbol="ETH",
        price_usd=Decimal("160"),
        market_cap_usd=Decimal("100000000"),
        volume_24h_usd=Decimal("15000000"),
        change_1h_pct=Decimal("4"),
        change_24h_pct=Decimal(move_24h_pct),
        raw={},
    )
    position = PositionMetrics(
        position_id=uuid4(),
        asset_id=quote.asset_id,
        symbol="ETH",
        asset_name="Ethereum",
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        cost_basis=Decimal("100"),
        current_price=Decimal("160"),
        position_value=Decimal("160"),
        unrealized_pnl_value=Decimal("60"),
        unrealized_pnl_pct=Decimal(pnl_pct),
        weight_pct=Decimal(weight_pct),
        move_15m_pct=None,
        move_1h_pct=Decimal("4"),
        move_4h_pct=Decimal("10"),
        move_24h_pct=Decimal(move_24h_pct),
        peak_price_48h=None,
        drop_from_peak_pct=None,
        market_cap_usd=Decimal("100000000"),
        volume_24h_usd=Decimal("15000000"),
        provider="test",
        quote=quote,
    )
    return PortfolioMetrics(
        portfolio_id=uuid4(),
        portfolio_name="Test",
        strategy_profile_code=StrategyProfileCode.MAIN,
        evaluated_at=datetime.now(timezone.utc),
        total_value=Decimal("160"),
        total_cost_basis=Decimal("100"),
        unrealized_pnl_value=Decimal("60"),
        unrealized_pnl_pct=Decimal("60"),
        portfolio_change_24h_pct=Decimal(move_24h_pct),
        positions=[position],
    )


def test_main_strategy_emits_take_profit_earlier() -> None:
    settings = Settings()
    strategy = get_strategy(StrategyProfileCode.MAIN, settings)

    signals = strategy.evaluate(Portfolio(name="Main"), _metrics("35", "14", "50"))

    assert any(signal.signal_type.value == "take_profit" for signal in signals)


def test_long_term_strategy_requires_more_extension_for_take_profit() -> None:
    settings = Settings()
    strategy = get_strategy(StrategyProfileCode.LONG_TERM, settings)

    signals = strategy.evaluate(Portfolio(name="Long-Term"), _metrics("80", "8", "20"))

    assert all(signal.signal_type.value != "take_profit" for signal in signals)
