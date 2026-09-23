from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.db.models import Portfolio
from app.providers.interfaces import MarketQuote
from app.services.market_service import MarketService
from app.services.types import DECIMAL_HUNDRED, DECIMAL_ZERO, PortfolioMetrics, PositionMetrics


class ValuationService:
    def __init__(self, market_service: MarketService) -> None:
        self.market_service = market_service

    def build_portfolio_metrics(
        self,
        portfolio: Portfolio,
        quotes: dict,
    ) -> PortfolioMetrics:
        positions: list[PositionMetrics] = []
        total_value = DECIMAL_ZERO
        total_cost_basis = DECIMAL_ZERO
        total_value_24h_ago = DECIMAL_ZERO
        total_value_24h_ago_complete = True

        for position in portfolio.positions:
            quote = quotes.get(position.asset_id)
            if quote is None:
                continue

            quantity = Decimal(position.quantity)
            average_entry_price = Decimal(position.average_entry_price)
            cost_basis = Decimal(position.cost_basis)
            current_price = Decimal(quote.price_usd)
            position_value = quantity * current_price
            unrealized_pnl_value = position_value - cost_basis
            unrealized_pnl_pct = (
                (unrealized_pnl_value / cost_basis) * DECIMAL_HUNDRED
                if cost_basis > DECIMAL_ZERO
                else None
            )

            move_15m_pct = self._compute_move_pct(
                current_price,
                self.market_service.historical_price_minutes(position.asset_id, minutes_ago=15),
            )
            move_1h_pct = self._compute_move_pct(
                current_price,
                self.market_service.historical_price(position.asset_id, hours_ago=1),
                fallback=quote.change_1h_pct,
            )
            move_4h_pct = self._compute_move_pct(
                current_price,
                self.market_service.historical_price(position.asset_id, hours_ago=4),
            )
            historical_24h_price = self.market_service.historical_price(position.asset_id, hours_ago=24)
            move_24h_pct = self._compute_move_pct(
                current_price,
                historical_24h_price,
                fallback=quote.change_24h_pct,
            )
            peak_price_48h = self.market_service.peak_price(position.asset_id, hours_lookback=48)
            drop_from_peak_pct = self._compute_drop_from_peak(current_price, peak_price_48h)

            total_value += position_value
            total_cost_basis += cost_basis

            if historical_24h_price is not None:
                total_value_24h_ago += historical_24h_price * quantity
            else:
                total_value_24h_ago_complete = False

            positions.append(
                PositionMetrics(
                    position_id=position.id,
                    asset_id=position.asset_id,
                    symbol=position.asset.symbol,
                    asset_name=position.asset.name,
                    quantity=quantity,
                    average_entry_price=average_entry_price,
                    cost_basis=cost_basis,
                    current_price=current_price,
                    position_value=position_value,
                    unrealized_pnl_value=unrealized_pnl_value,
                    unrealized_pnl_pct=unrealized_pnl_pct,
                    weight_pct=None,
                    move_15m_pct=move_15m_pct,
                    move_1h_pct=move_1h_pct,
                    move_4h_pct=move_4h_pct,
                    move_24h_pct=move_24h_pct,
                    peak_price_48h=peak_price_48h,
                    drop_from_peak_pct=drop_from_peak_pct,
                    market_cap_usd=quote.market_cap_usd,
                    volume_24h_usd=quote.volume_24h_usd,
                    provider=quote.provider,
                    quote=quote,
                )
            )

        for metrics in positions:
            metrics.weight_pct = (
                (metrics.position_value / total_value) * DECIMAL_HUNDRED
                if total_value > DECIMAL_ZERO
                else None
            )

        unrealized_pnl_value = total_value - total_cost_basis
        unrealized_pnl_pct = (
            (unrealized_pnl_value / total_cost_basis) * DECIMAL_HUNDRED
            if total_cost_basis > DECIMAL_ZERO
            else None
        )

        portfolio_change_24h_pct = None
        if total_value_24h_ago_complete and total_value_24h_ago > DECIMAL_ZERO:
            portfolio_change_24h_pct = (
                (total_value - total_value_24h_ago) / total_value_24h_ago
            ) * DECIMAL_HUNDRED
        elif positions:
            weighted_change = DECIMAL_ZERO
            total_weight = DECIMAL_ZERO
            for metrics in positions:
                if metrics.move_24h_pct is None or metrics.weight_pct is None:
                    continue
                weighted_change += metrics.move_24h_pct * metrics.weight_pct
                total_weight += metrics.weight_pct
            if total_weight > DECIMAL_ZERO:
                portfolio_change_24h_pct = weighted_change / total_weight

        sorted_by_move = sorted(
            [position for position in positions if position.move_24h_pct is not None],
            key=lambda item: item.move_24h_pct,
        )
        top_losers = [
            f"{position.symbol} {position.move_24h_pct:.2f}%"
            for position in sorted_by_move[:3]
        ]
        top_gainers = [
            f"{position.symbol} {position.move_24h_pct:.2f}%"
            for position in reversed(sorted_by_move[-3:])
        ]

        concentration_warnings = [
            f"{position.symbol} at {position.weight_pct:.2f}% of portfolio value"
            for position in positions
            if position.weight_pct is not None and position.weight_pct >= Decimal("35")
        ]

        return PortfolioMetrics(
            portfolio_id=portfolio.id,
            portfolio_name=portfolio.name,
            strategy_profile_code=portfolio.strategy_profile.code,
            evaluated_at=datetime.now(timezone.utc),
            total_value=total_value,
            total_cost_basis=total_cost_basis,
            unrealized_pnl_value=unrealized_pnl_value,
            unrealized_pnl_pct=unrealized_pnl_pct,
            portfolio_change_24h_pct=portfolio_change_24h_pct,
            positions=sorted(positions, key=lambda item: item.position_value, reverse=True),
            top_gainers=top_gainers,
            top_losers=top_losers,
            concentration_warnings=concentration_warnings,
        )

    @staticmethod
    def _compute_move_pct(
        current_price: Decimal,
        reference_price: Decimal | None,
        *,
        fallback: Decimal | None = None,
    ) -> Decimal | None:
        if reference_price is not None and reference_price > DECIMAL_ZERO:
            return ((current_price - reference_price) / reference_price) * DECIMAL_HUNDRED
        return fallback

    @staticmethod
    def _compute_drop_from_peak(current_price: Decimal, peak_price: Decimal | None) -> Decimal | None:
        if peak_price is None or peak_price <= DECIMAL_ZERO:
            return None
        if current_price >= peak_price:
            return DECIMAL_ZERO
        return ((peak_price - current_price) / peak_price) * DECIMAL_HUNDRED
