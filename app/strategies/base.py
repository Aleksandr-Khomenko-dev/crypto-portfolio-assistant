from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from app.config import Settings, TakeProfitBand
from app.db.models import Portfolio, SignalSeverity, SignalType, StrategyProfileCode
from app.services.types import (
    DECIMAL_HUNDRED,
    PortfolioMetrics,
    PositionMetrics,
    SignalCandidate,
)


@dataclass(frozen=True, slots=True)
class StrategyRuleSet:
    profile_code: StrategyProfileCode
    concentration_alert_pct: Decimal
    drawdown_alert_pct: Decimal
    take_profit_bands: tuple[TakeProfitBand, ...]
    take_profit_requires_extension: bool


class BaseStrategy:
    SEVERITY_RANK = {
        SignalSeverity.LOW: 1,
        SignalSeverity.MEDIUM: 2,
        SignalSeverity.HIGH: 3,
    }

    def __init__(self, settings: Settings, rules: StrategyRuleSet) -> None:
        self.settings = settings
        self.rules = rules

    def evaluate(
        self, portfolio: Portfolio, metrics: PortfolioMetrics
    ) -> list[SignalCandidate]:
        signals: list[SignalCandidate] = []

        for position in metrics.positions:
            # Skip noise: positions too small to matter
            if self._is_too_small(position):
                continue

            extreme_pump_signal = self._extreme_pump_signal(position)
            if extreme_pump_signal:
                signals.append(extreme_pump_signal)

            # Suppress the duplicate rise alert, while retaining independent TP and risk checks.
            rise_signal = (
                None if extreme_pump_signal else self._abnormal_rise_signal(position)
            )
            if rise_signal:
                signals.append(rise_signal)

            drop_signal = self._abnormal_drop_signal(position)
            if drop_signal:
                signals.append(drop_signal)

            pullback_signal = self._pullback_signal(position)
            if pullback_signal:
                signals.append(pullback_signal)

            take_profit_signal = self._take_profit_signal(position)
            if take_profit_signal:
                signals.append(take_profit_signal)

            signals.extend(self._risk_signals(position))

        portfolio_risk_signal = self._portfolio_risk_signal(metrics)
        if portfolio_risk_signal:
            signals.append(portfolio_risk_signal)

        return signals

    def _is_too_small(self, position: PositionMetrics) -> bool:
        return (
            position.weight_pct is not None
            and position.weight_pct < self.settings.min_position_weight_pct
        )

    def _abnormal_rise_signal(
        self, position: PositionMetrics
    ) -> SignalCandidate | None:
        severity, timeframe, move_pct = self._resolve_rise_signal(position)
        if severity is None or timeframe is None or move_pct is None:
            return None

        confidence = self._confidence_for_move(position, severity, direction="up")
        return SignalCandidate(
            signal_type=SignalType.ABNORMAL_RISE,
            severity=severity,
            confidence_score=confidence,
            title=f"{position.symbol} abnormal rise",
            message=f"{position.symbol} is up {move_pct:.2f}% on the {timeframe} lookback.",
            action_idea="Review whether momentum is stretched and whether a trim should be staged.",
            reasoning=(
                f"Multiple monitoring thresholds flagged upside acceleration on the {timeframe} window."
            ),
            risk_note="Fast upside can reverse sharply after crowded moves.",
            explanation=(
                f"The system classified this as a {severity.value} upside signal with {confidence:.2f} confidence "
                f"based on recent price acceleration and available market context."
            ),
            event_key=f"rise:{position.position_id}:{timeframe}:{severity.value}",
            cooldown_minutes=self.settings.signal_cooldown_minutes,
            asset_id=position.asset_id,
            position_id=position.position_id,
            metrics_json={
                "timeframe": timeframe,
                "move_pct": str(move_pct),
                "weight_pct": str(position.weight_pct)
                if position.weight_pct is not None
                else None,
            },
        )

    def _extreme_pump_signal(self, position: PositionMetrics) -> SignalCandidate | None:
        s = self.settings
        triggered_by: list[str] = []

        if (
            position.move_1h_pct is not None
            and position.move_1h_pct >= s.extreme_pump_1h_pct
        ):
            triggered_by.append(f"+{position.move_1h_pct:.1f}% in 1h")
        if (
            position.move_4h_pct is not None
            and position.move_4h_pct >= s.extreme_pump_4h_pct
        ):
            triggered_by.append(f"+{position.move_4h_pct:.1f}% in 4h")
        if (
            position.move_24h_pct is not None
            and position.move_24h_pct >= s.extreme_pump_24h_pct
        ):
            triggered_by.append(f"+{position.move_24h_pct:.1f}% in 24h")

        if not triggered_by:
            return None

        trigger_str = " | ".join(triggered_by)
        return SignalCandidate(
            signal_type=SignalType.EXTREME_PUMP,
            severity=SignalSeverity.HIGH,
            confidence_score=Decimal("0.91"),
            title=f"{position.symbol} extreme pump detected",
            message=f"{position.symbol} is showing parabolic price action: {trigger_str}.",
            action_idea=(
                "This is NOT a buy signal. Consider taking partial profits (15–25% of position) "
                "to reduce overextension risk. Do NOT chase the move."
            ),
            reasoning=(
                f"Price has accelerated beyond extreme-pump thresholds: {trigger_str}. "
                "Parabolic moves have high reversal probability."
            ),
            risk_note=(
                "Extreme pumps often end with sharp corrections of 30–50%. "
                "Chasing momentum at this stage carries maximum risk."
            ),
            explanation=(
                f"{position.symbol} crossed the extreme-pump threshold. "
                "The system flagged this as overheating, not as a continuation signal."
            ),
            event_key=f"extreme-pump:{position.position_id}:{trigger_str[:40]}",
            cooldown_minutes=self.settings.signal_cooldown_minutes,
            asset_id=position.asset_id,
            position_id=position.position_id,
            metrics_json={
                "triggered_by": triggered_by,
                "move_1h_pct": str(position.move_1h_pct)
                if position.move_1h_pct is not None
                else None,
                "move_4h_pct": str(position.move_4h_pct)
                if position.move_4h_pct is not None
                else None,
                "move_24h_pct": str(position.move_24h_pct)
                if position.move_24h_pct is not None
                else None,
            },
        )

    def _pullback_signal(self, position: PositionMetrics) -> SignalCandidate | None:
        drop = position.drop_from_peak_pct
        if drop is None or drop < self.settings.pullback_healthy_pct:
            return None

        s = self.settings
        if drop >= s.pullback_weak_pct:
            severity = SignalSeverity.HIGH
            zone = "possible trend weakness"
            action = (
                "Do NOT treat this as a buy signal. Verify if the thesis still holds. "
                "Consider reducing exposure if fundamentals have changed."
            )
        elif drop >= s.pullback_caution_pct:
            severity = SignalSeverity.MEDIUM
            zone = "caution zone"
            action = "Watch for stabilization before considering any action. No guarantees of recovery."
        else:
            severity = SignalSeverity.LOW
            zone = "healthy pullback — watch zone"
            action = (
                "Normal retracement after a move. Monitor for support confirmation."
            )

        return SignalCandidate(
            signal_type=SignalType.PULLBACK,
            severity=severity,
            confidence_score=Decimal("0.70"),
            title=f"{position.symbol} pullback from peak: {zone}",
            message=(
                f"{position.symbol} is down {drop:.1f}% from its {s.pullback_lookback_hours}h high."
            ),
            action_idea=action,
            reasoning=(
                f"Price has retraced {drop:.1f}% from the local peak observed over the last "
                f"{s.pullback_lookback_hours} hours."
            ),
            risk_note="Pullbacks can deepen. This is an observation signal, not a re-entry recommendation.",
            explanation=(
                f"The system classified this pullback as '{zone}' based on the {drop:.1f}% drop from peak. "
                f"Thresholds: healthy <{s.pullback_caution_pct}%, caution <{s.pullback_weak_pct}%, weak >{s.pullback_weak_pct}%."
            ),
            event_key=f"pullback:{position.position_id}:{zone.split()[0]}",
            cooldown_minutes=self.settings.signal_cooldown_minutes,
            asset_id=position.asset_id,
            position_id=position.position_id,
            metrics_json={
                "drop_from_peak_pct": str(drop),
                "peak_price_48h": str(position.peak_price_48h)
                if position.peak_price_48h
                else None,
                "zone": zone,
            },
        )

    def _abnormal_drop_signal(
        self, position: PositionMetrics
    ) -> SignalCandidate | None:
        severity, timeframe, move_pct = self._resolve_drop_signal(position)
        if severity is None or timeframe is None or move_pct is None:
            return None

        dangerous = self._is_dangerous_weakness(position)
        confidence = self._confidence_for_move(position, severity, direction="down")
        weakness_label = "dangerous weakness" if dangerous else "normal pullback"

        return SignalCandidate(
            signal_type=SignalType.ABNORMAL_DROP,
            severity=severity if dangerous else SignalSeverity.LOW,
            confidence_score=confidence,
            title=f"{position.symbol} downside pressure",
            message=f"{position.symbol} is down {abs(move_pct):.2f}% on the {timeframe} lookback.",
            action_idea=(
                "Check if the thesis still holds before making any manual portfolio changes."
                if dangerous
                else "Monitor for stabilization; this still looks like a manageable pullback."
            ),
            reasoning=(
                f"Current pattern resembles {weakness_label} based on multi-timeframe move alignment and PnL context."
            ),
            risk_note="Downside moves can compound quickly when several timeframes turn negative together.",
            explanation=(
                f"The move was classified as {weakness_label}. Confidence is {confidence:.2f}, with severity "
                f"driven by the strongest negative timeframe and your current exposure."
            ),
            event_key=f"drop:{position.position_id}:{timeframe}:{dangerous}",
            cooldown_minutes=self.settings.signal_cooldown_minutes,
            asset_id=position.asset_id,
            position_id=position.position_id,
            metrics_json={
                "timeframe": timeframe,
                "move_pct": str(move_pct),
                "dangerous": dangerous,
            },
        )

    def _take_profit_signal(self, position: PositionMetrics) -> SignalCandidate | None:
        if position.unrealized_pnl_pct is None:
            return None

        eligible_band = self._highest_take_profit_band(position.unrealized_pnl_pct)
        if eligible_band is None:
            return None

        if self.rules.take_profit_requires_extension:
            extension = max(
                abs(position.move_24h_pct or Decimal("0")),
                abs(position.move_4h_pct or Decimal("0")),
            )
            if extension < self.settings.abnormal_rise_24h_medium_pct and (
                position.weight_pct or Decimal("0")
            ) < (self.rules.concentration_alert_pct - Decimal("5")):
                return None

        return SignalCandidate(
            signal_type=SignalType.TAKE_PROFIT,
            severity=SignalSeverity.MEDIUM,
            confidence_score=Decimal("0.75"),
            title=f"{position.symbol} take-profit band reached",
            message=(
                f"{position.symbol} is up {position.unrealized_pnl_pct:.2f}% from average entry."
            ),
            action_idea=(
                f"Consider manually trimming {eligible_band.sell_min_pct:.0f}% to "
                f"{eligible_band.sell_max_pct:.0f}% of the position."
            ),
            reasoning=(
                f"The active band starts at +{eligible_band.gain_trigger_pct:.0f}% and the position is now inside it."
            ),
            risk_note="This is a staged de-risking suggestion only. No trade will be executed by the app.",
            explanation=(
                f"The strategy profile recommends partial profit-taking here to reduce concentration and protect gains."
            ),
            event_key=f"take-profit:{position.position_id}:{eligible_band.gain_trigger_pct}",
            cooldown_minutes=self.settings.take_profit_cooldown_days * 24 * 60,
            asset_id=position.asset_id,
            position_id=position.position_id,
            metrics_json={
                "gain_trigger_pct": str(eligible_band.gain_trigger_pct),
                "sell_min_pct": str(eligible_band.sell_min_pct),
                "sell_max_pct": str(eligible_band.sell_max_pct),
            },
        )

    def _risk_signals(self, position: PositionMetrics) -> list[SignalCandidate]:
        signals: list[SignalCandidate] = []

        if (
            position.weight_pct is not None
            and position.weight_pct >= self.rules.concentration_alert_pct
        ):
            signals.append(
                SignalCandidate(
                    signal_type=SignalType.RISK,
                    severity=SignalSeverity.HIGH,
                    confidence_score=Decimal("0.88"),
                    title=f"{position.symbol} concentration risk",
                    message=(
                        f"{position.symbol} represents {position.weight_pct:.2f}% of portfolio value."
                    ),
                    action_idea="Review whether this single-asset weight still matches your portfolio plan.",
                    reasoning=(
                        f"This exceeds the {self.rules.concentration_alert_pct:.0f}% concentration threshold for this profile."
                    ),
                    risk_note="Large single-name exposure can dominate portfolio outcomes in both directions.",
                    explanation="The portfolio has become too dependent on one asset.",
                    event_key=f"risk-concentration:{position.position_id}",
                    cooldown_minutes=self.settings.risk_cooldown_minutes,
                    asset_id=position.asset_id,
                    position_id=position.position_id,
                    metrics_json={"weight_pct": str(position.weight_pct)},
                )
            )

        if position.unrealized_pnl_pct is not None and position.unrealized_pnl_pct <= (
            self.rules.drawdown_alert_pct * Decimal("-1")
        ):
            signals.append(
                SignalCandidate(
                    signal_type=SignalType.RISK,
                    severity=SignalSeverity.HIGH,
                    confidence_score=Decimal("0.90"),
                    title=f"{position.symbol} drawdown risk",
                    message=(
                        f"{position.symbol} is down {abs(position.unrealized_pnl_pct):.2f}% from average entry."
                    ),
                    action_idea="Review the downside thesis and decide whether the size still makes sense.",
                    reasoning=(
                        f"The drawdown is deeper than the {self.rules.drawdown_alert_pct:.0f}% threshold for this profile."
                    ),
                    risk_note="Deep drawdowns can impair recovery and increase emotional decision-making.",
                    explanation="This position is materially underwater relative to the selected strategy profile.",
                    event_key=f"risk-drawdown:{position.position_id}",
                    cooldown_minutes=self.settings.risk_cooldown_minutes,
                    asset_id=position.asset_id,
                    position_id=position.position_id,
                    metrics_json={
                        "unrealized_pnl_pct": str(position.unrealized_pnl_pct)
                    },
                )
            )

        high_volatility = max(
            abs(position.move_24h_pct or Decimal("0")),
            abs(position.move_4h_pct or Decimal("0")),
        )
        if high_volatility >= self.settings.high_volatility_24h_pct:
            signals.append(
                SignalCandidate(
                    signal_type=SignalType.RISK,
                    severity=SignalSeverity.MEDIUM,
                    confidence_score=Decimal("0.72"),
                    title=f"{position.symbol} volatility risk",
                    message=f"{position.symbol} is showing elevated short-term volatility.",
                    action_idea="Keep sizing discipline and avoid assuming that the latest move will persist.",
                    reasoning="Recent 4h/24h moves are large enough to materially change risk.",
                    risk_note="High volatility increases both upside opportunity and downside execution risk.",
                    explanation="This signal exists to slow down decision-making when price action becomes unstable.",
                    event_key=f"risk-volatility:{position.position_id}",
                    cooldown_minutes=self.settings.risk_cooldown_minutes,
                    asset_id=position.asset_id,
                    position_id=position.position_id,
                    metrics_json={"volatility_pct": str(high_volatility)},
                )
            )

        return signals

    def _portfolio_risk_signal(
        self, metrics: PortfolioMetrics
    ) -> SignalCandidate | None:
        if (
            self.rules.profile_code != StrategyProfileCode.LONG_TERM
            or len(metrics.positions) < 2
        ):
            return None

        top_two_weight = sum(
            (position.weight_pct or Decimal("0")) for position in metrics.positions[:2]
        )
        if top_two_weight < Decimal("65"):
            return None

        return SignalCandidate(
            signal_type=SignalType.RISK,
            severity=SignalSeverity.HIGH,
            confidence_score=Decimal("0.86"),
            title="Long-term portfolio is becoming aggressive",
            message=f"The top two positions now account for {top_two_weight:.2f}% of portfolio value.",
            action_idea="Review whether the kids portfolio is still aligned with capital-preservation goals.",
            reasoning="The long-term profile should stay broadly diversified and lower-noise than the main book.",
            risk_note="Concentration drift can quietly turn a conservative portfolio into a directional bet.",
            explanation="This portfolio-level warning is specific to the conservative long-term strategy.",
            event_key=f"risk-long-term-aggressive:{metrics.portfolio_id}",
            cooldown_minutes=self.settings.risk_cooldown_minutes,
            metrics_json={"top_two_weight_pct": str(top_two_weight)},
        )

    def _resolve_rise_signal(
        self,
        position: PositionMetrics,
    ) -> tuple[SignalSeverity | None, str | None, Decimal | None]:
        t = self.settings.abnormal_rise_15m_pct
        checks = (
            ("15m", position.move_15m_pct, t, t, t),  # single threshold for 15m
            (
                "1h",
                position.move_1h_pct,
                self.settings.abnormal_rise_1h_low_pct,
                self.settings.abnormal_rise_1h_medium_pct,
                self.settings.abnormal_rise_1h_high_pct,
            ),
            (
                "4h",
                position.move_4h_pct,
                self.settings.abnormal_rise_4h_low_pct,
                self.settings.abnormal_rise_4h_medium_pct,
                self.settings.abnormal_rise_4h_high_pct,
            ),
            (
                "24h",
                position.move_24h_pct,
                self.settings.abnormal_rise_24h_low_pct,
                self.settings.abnormal_rise_24h_medium_pct,
                self.settings.abnormal_rise_24h_high_pct,
            ),
        )
        return self._resolve_move(checks, direction="up")

    def _resolve_drop_signal(
        self,
        position: PositionMetrics,
    ) -> tuple[SignalSeverity | None, str | None, Decimal | None]:
        checks = (
            (
                "1h",
                position.move_1h_pct,
                self.settings.abnormal_drop_1h_low_pct,
                self.settings.abnormal_drop_1h_medium_pct,
                self.settings.abnormal_drop_1h_high_pct,
            ),
            (
                "4h",
                position.move_4h_pct,
                self.settings.abnormal_drop_4h_low_pct,
                self.settings.abnormal_drop_4h_medium_pct,
                self.settings.abnormal_drop_4h_high_pct,
            ),
            (
                "24h",
                position.move_24h_pct,
                self.settings.abnormal_drop_24h_low_pct,
                self.settings.abnormal_drop_24h_medium_pct,
                self.settings.abnormal_drop_24h_high_pct,
            ),
        )
        return self._resolve_move(checks, direction="down")

    @staticmethod
    def _resolve_move(
        checks: Iterable[tuple[str, Decimal | None, Decimal, Decimal, Decimal]],
        *,
        direction: str,
    ) -> tuple[SignalSeverity | None, str | None, Decimal | None]:
        strongest: tuple[SignalSeverity, str, Decimal] | None = None

        for timeframe, value, low, medium, high in checks:
            if value is None:
                continue
            magnitude = value if direction == "up" else abs(value)
            direction_ok = value > 0 if direction == "up" else value < 0
            if not direction_ok:
                continue

            severity = None
            if magnitude >= high:
                severity = SignalSeverity.HIGH
            elif magnitude >= medium:
                severity = SignalSeverity.MEDIUM
            elif magnitude >= low:
                severity = SignalSeverity.LOW

            if severity is None:
                continue

            if (
                strongest is None
                or BaseStrategy.SEVERITY_RANK[strongest[0]]
                < BaseStrategy.SEVERITY_RANK[severity]
            ):
                strongest = (severity, timeframe, value)
            elif strongest[0] == severity and abs(value) > abs(strongest[2]):
                strongest = (severity, timeframe, value)

        if strongest is None:
            return None, None, None
        return strongest

    def _is_dangerous_weakness(self, position: PositionMetrics) -> bool:
        down_4h = (position.move_4h_pct or Decimal("0")) < 0
        down_24h = (position.move_24h_pct or Decimal("0")) < 0
        underwater = (
            position.unrealized_pnl_pct is not None
            and position.unrealized_pnl_pct
            <= (self.rules.drawdown_alert_pct * Decimal("-0.5"))
        )
        return (down_4h and down_24h) or underwater

    def _highest_take_profit_band(self, pnl_pct: Decimal) -> TakeProfitBand | None:
        eligible = [
            band
            for band in self.rules.take_profit_bands
            if pnl_pct >= band.gain_trigger_pct
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda band: band.gain_trigger_pct)

    def _confidence_for_move(
        self,
        position: PositionMetrics,
        severity: SignalSeverity,
        *,
        direction: str,
    ) -> Decimal:
        base = {
            SignalSeverity.LOW: Decimal("0.58"),
            SignalSeverity.MEDIUM: Decimal("0.73"),
            SignalSeverity.HIGH: Decimal("0.87"),
        }[severity]

        aligned_count = 0
        for value in (
            position.move_1h_pct,
            position.move_4h_pct,
            position.move_24h_pct,
        ):
            if value is None:
                continue
            if direction == "up" and value > 0:
                aligned_count += 1
            if direction == "down" and value < 0:
                aligned_count += 1
        if aligned_count >= 2:
            base += Decimal("0.05")
        if position.volume_24h_usd is not None:
            base += Decimal("0.02")
        return min(base, Decimal("0.99"))
