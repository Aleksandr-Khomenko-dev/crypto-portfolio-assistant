from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import AlertChannel, Portfolio, Signal
from app.providers.interfaces import AbstractMarketDataProvider
from app.schemas.common import DailyDigestRead, SignalRead
from app.schemas.monitoring import BulkMonitorResponse, DigestResult, PortfolioEvaluationRead, PortfolioMonitorResult, PositionMetricsRead
from app.services.alert_service import AlertService
from app.services.digest_service import DigestService
from app.services.market_service import MarketService
from app.services.portfolio_service import PortfolioService
from app.services.price_alert_service import build_price_level_alert_candidates
from app.services.signal_service import SignalService
from app.services.telegram_service import TelegramService
from app.services.types import PortfolioMetrics
from app.services.valuation_service import ValuationService
from app.strategies import get_strategy

logger = logging.getLogger(__name__)


class MonitoringService:
    def __init__(
        self,
        session: Session,
        market_provider: AbstractMarketDataProvider,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.market_provider = market_provider
        self.portfolio_service = PortfolioService(session)
        self.market_service = MarketService(session, market_provider)
        self.valuation_service = ValuationService(self.market_service)
        self.signal_service = SignalService(session)
        self.alert_service = AlertService(session)
        self.digest_service = DigestService(session)
        self.telegram_service = TelegramService(self.settings)

    async def evaluate_portfolio(self, portfolio: Portfolio, *, run_reason: str) -> PortfolioMonitorResult:
        metrics, signals = await self._run_evaluation(portfolio)
        return PortfolioMonitorResult(
            portfolio_id=portfolio.id,
            portfolio_name=portfolio.name,
            signal_count=len(signals),
            evaluation=self._to_evaluation_schema(metrics),
            generated_signals=[SignalRead.model_validate(signal) for signal in signals],
        )

    async def preview_portfolio(self, portfolio: Portfolio) -> PortfolioMetrics:
        quotes = await self.market_service.fetch_quotes_for_portfolio(portfolio)
        self.market_service.persist_quotes(quotes)
        metrics = self.valuation_service.build_portfolio_metrics(portfolio, quotes)
        strategy = get_strategy(portfolio.strategy_profile.code, self.settings)
        preview_candidates = strategy.evaluate(portfolio, metrics)
        metrics.action_summary = self.digest_service.build_action_summary([], metrics)
        if preview_candidates:
            metrics.action_summary = preview_candidates[0].action_idea
        self.session.commit()
        return metrics

    async def evaluate_all_portfolios(self, run_reason: str) -> BulkMonitorResponse:
        results: list[PortfolioMonitorResult] = []
        for portfolio in self.portfolio_service.list_portfolios():
            try:
                results.append(await self.evaluate_portfolio(portfolio, run_reason=run_reason))
            except Exception:
                logger.exception("Portfolio evaluation failed for %s", portfolio.name)
                self.session.rollback()

        return BulkMonitorResponse(
            run_reason=run_reason,
            completed_at=datetime.now(self.settings.tzinfo),
            results=results,
        )

    async def create_morning_digest(self, portfolio: Portfolio) -> DigestResult:
        metrics, signals = await self._run_evaluation(portfolio)
        recent_signals = self.signal_service.list_recent_signals(portfolio.id, limit=5)
        content, summary_json = self.digest_service.build_digest_text(portfolio, metrics, recent_signals)
        digest = self.digest_service.create_digest(portfolio, content, summary_json)
        self.session.commit()

        sent = False
        if portfolio.morning_digest_enabled:
            sent = await self.telegram_service.send_digest(portfolio, digest)
            if sent:
                digest.sent_to_telegram = True
                self.session.add(digest)
                self.session.commit()

        return DigestResult(
            portfolio_id=portfolio.id,
            digest=DailyDigestRead.model_validate(digest),
            sent_to_telegram=sent,
        )

    async def create_all_morning_digests(self) -> list[DigestResult]:
        results: list[DigestResult] = []
        for portfolio in self.portfolio_service.list_portfolios():
            try:
                results.append(await self.create_morning_digest(portfolio))
            except Exception:
                logger.exception("Morning digest generation failed for %s", portfolio.name)
                self.session.rollback()
        return results

    async def _run_evaluation(self, portfolio: Portfolio) -> tuple[PortfolioMetrics, list[Signal]]:
        """Core evaluation: fetch quotes, compute metrics, generate and persist signals, send alerts."""
        quotes = await self.market_service.fetch_quotes_for_portfolio(portfolio)
        self.market_service.persist_quotes(quotes)
        metrics = self.valuation_service.build_portfolio_metrics(portfolio, quotes)
        strategy = get_strategy(portfolio.strategy_profile.code, self.settings)
        candidates = list(strategy.evaluate(portfolio, metrics))
        candidates.extend(build_price_level_alert_candidates(portfolio, metrics))
        signals = self.signal_service.create_signals(portfolio, candidates)
        metrics.action_summary = self.digest_service.build_action_summary(signals, metrics)
        self.session.commit()

        if signals and portfolio.alerts_enabled:
            events = self.alert_service.create_events(
                portfolio,
                signals,
                channel=AlertChannel.TELEGRAM,
                destination=portfolio.telegram_chat_id,
            )
            try:
                sent = await self.telegram_service.send_signals(portfolio, signals)
                if sent:
                    self.alert_service.mark_sent(events)
                else:
                    self.alert_service.mark_failed(events, "Telegram disabled or chat not configured")
            except Exception as exc:  # pragma: no cover - network handling
                logger.exception("Telegram alert delivery failed for %s", portfolio.name)
                self.alert_service.mark_failed(events, str(exc))
            self.session.commit()

        return metrics, signals

    @staticmethod
    def _to_evaluation_schema(metrics: PortfolioMetrics) -> PortfolioEvaluationRead:
        return PortfolioEvaluationRead(
            portfolio_id=metrics.portfolio_id,
            portfolio_name=metrics.portfolio_name,
            strategy_profile_code=metrics.strategy_profile_code,
            evaluated_at=metrics.evaluated_at,
            total_value=metrics.total_value,
            total_cost_basis=metrics.total_cost_basis,
            unrealized_pnl_value=metrics.unrealized_pnl_value,
            unrealized_pnl_pct=metrics.unrealized_pnl_pct,
            portfolio_change_24h_pct=metrics.portfolio_change_24h_pct,
            positions=[
                PositionMetricsRead(
                    position_id=position.position_id,
                    asset_id=position.asset_id,
                    symbol=position.symbol,
                    asset_name=position.asset_name,
                    quantity=position.quantity,
                    average_entry_price=position.average_entry_price,
                    cost_basis=position.cost_basis,
                    current_price=position.current_price,
                    position_value=position.position_value,
                    unrealized_pnl_value=position.unrealized_pnl_value,
                    unrealized_pnl_pct=position.unrealized_pnl_pct,
                    weight_pct=position.weight_pct,
                    move_15m_pct=position.move_15m_pct,
                    move_1h_pct=position.move_1h_pct,
                    move_4h_pct=position.move_4h_pct,
                    move_24h_pct=position.move_24h_pct,
                    peak_price_48h=position.peak_price_48h,
                    drop_from_peak_pct=position.drop_from_peak_pct,
                    market_cap_usd=position.market_cap_usd,
                    volume_24h_usd=position.volume_24h_usd,
                    provider=position.provider,
                )
                for position in metrics.positions
            ],
            top_gainers=metrics.top_gainers,
            top_losers=metrics.top_losers,
            action_summary=metrics.action_summary,
            concentration_warnings=metrics.concentration_warnings,
        )
