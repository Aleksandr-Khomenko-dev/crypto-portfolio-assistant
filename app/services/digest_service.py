from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import DailyDigest, Portfolio, Signal, SignalType
from app.services.types import PortfolioMetrics

_STATUS_CALM = "CALM"
_STATUS_WATCH = "WATCH"
_STATUS_RISK = "RISK"
_STATUS_OPPORTUNITY = "OPPORTUNITY"


class DigestService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def portfolio_status(self, signals: list[Signal], metrics: PortfolioMetrics) -> str:
        """Return one of: CALM / WATCH / RISK / OPPORTUNITY."""
        has_high = any(s.severity.value == "high" for s in signals)
        has_extreme_pump = any(
            s.signal_type == SignalType.EXTREME_PUMP for s in signals
        )
        has_take_profit = any(s.signal_type == SignalType.TAKE_PROFIT for s in signals)
        has_pullback = any(s.signal_type == SignalType.PULLBACK for s in signals)

        if has_high or metrics.concentration_warnings:
            return _STATUS_RISK
        if has_extreme_pump:
            return _STATUS_RISK
        if has_take_profit:
            return _STATUS_OPPORTUNITY
        if has_pullback or signals:
            return _STATUS_WATCH
        return _STATUS_CALM

    def build_action_summary(
        self, signals: list[Signal], metrics: PortfolioMetrics
    ) -> str:
        status = self.portfolio_status(signals, metrics)
        if status == _STATUS_RISK:
            return "RISK — Требует внимания. Проверь концентрацию и активные алерты."
        if status == _STATUS_OPPORTUNITY:
            return "OPPORTUNITY — Достигнута зона тейк-профита по одной или нескольким позициям."
        if status == _STATUS_WATCH:
            return "WATCH — Активны тактические сигналы. Рассмотри фиксацию и снижение риска."
        return "CALM — Портфель стабилен. Продолжай мониторинг."

    def build_digest_text(
        self,
        portfolio: Portfolio,
        metrics: PortfolioMetrics,
        recent_signals: list[Signal],
    ) -> tuple[str, dict[str, object]]:
        status = self.portfolio_status(recent_signals, metrics)
        change_str = (
            f"{metrics.portfolio_change_24h_pct:+.2f}%"
            if metrics.portfolio_change_24h_pct is not None
            else "n/a"
        )
        pnl_sign = "+" if metrics.unrealized_pnl_value >= 0 else ""

        lines = [
            f"{portfolio.name} morning digest",
            f"Status: [{status}]",
            f"Profile: {portfolio.strategy_profile.name}",
            f"Value: ${metrics.total_value:,.2f}  |  24h: {change_str}",
            f"P&L: {pnl_sign}${metrics.unrealized_pnl_value:,.2f}"
            + (
                f" ({metrics.unrealized_pnl_pct:+.2f}%)"
                if metrics.unrealized_pnl_pct is not None
                else ""
            ),
            "",
            "Top gainers:",
        ]
        lines.extend(
            [f"  {item}" for item in metrics.top_gainers] or ["  No gainers yet"]
        )
        lines.extend(["", "Top losers:"])
        lines.extend(
            [f"  {item}" for item in metrics.top_losers] or ["  No losers yet"]
        )

        # Assets near take-profit zone
        near_tp = [
            p
            for p in metrics.positions
            if p.unrealized_pnl_pct is not None
            and p.unrealized_pnl_pct >= Decimal("25")
        ]
        if near_tp:
            lines.extend(["", "Near take-profit zone:"])
            for p in near_tp:
                lines.append(f"  {p.symbol} +{p.unrealized_pnl_pct:.1f}% from entry")

        # Pullback watch
        in_pullback = [
            p
            for p in metrics.positions
            if p.drop_from_peak_pct is not None and p.drop_from_peak_pct >= Decimal("8")
        ]
        if in_pullback:
            lines.extend(["", "Pullback watch:"])
            for p in in_pullback:
                lines.append(f"  {p.symbol} -{p.drop_from_peak_pct:.1f}% from 48h high")

        if metrics.concentration_warnings:
            lines.extend(["", "Concentration warnings:"])
            lines.extend([f"  {warning}" for warning in metrics.concentration_warnings])

        lines.extend(["", f"Summary: {metrics.action_summary}", "", "Recent signals:"])
        if recent_signals:
            lines.extend(
                [
                    f"  [{s.severity.value.upper()}] {s.title}"
                    for s in recent_signals[:5]
                ]
            )
        else:
            lines.append("  No fresh signals.")

        lines.extend(["", "Read-only monitor. No trades were executed."])

        summary_json: dict[str, object] = {
            "status": status,
            "total_value": str(metrics.total_value),
            "unrealized_pnl_value": str(metrics.unrealized_pnl_value),
            "unrealized_pnl_pct": str(metrics.unrealized_pnl_pct)
            if metrics.unrealized_pnl_pct is not None
            else None,
            "portfolio_change_24h_pct": (
                str(metrics.portfolio_change_24h_pct)
                if metrics.portfolio_change_24h_pct is not None
                else None
            ),
            "top_gainers": metrics.top_gainers,
            "top_losers": metrics.top_losers,
            "concentration_warnings": metrics.concentration_warnings,
            "action_summary": metrics.action_summary,
        }
        return "\n".join(lines), summary_json

    def create_digest(
        self,
        portfolio: Portfolio,
        content: str,
        summary_json: dict[str, object],
    ) -> DailyDigest:
        digest = DailyDigest(
            portfolio_id=portfolio.id, content=content, summary_json=summary_json
        )
        self.session.add(digest)
        self.session.flush()
        return digest
