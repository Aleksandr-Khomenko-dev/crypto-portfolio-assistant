from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Portfolio, Signal
from app.services.types import SignalCandidate


class SignalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_recent_signals(self, portfolio_id: UUID, limit: int = 20) -> list[Signal]:
        statement = (
            select(Signal)
            .where(Signal.portfolio_id == portfolio_id)
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def get_signal(self, signal_id: UUID) -> Signal | None:
        return self.session.get(Signal, signal_id)

    def create_signals(self, portfolio: Portfolio, candidates: list[SignalCandidate]) -> list[Signal]:
        created: list[Signal] = []
        for candidate in candidates:
            if candidate.event_key and self._signal_exists(portfolio.id, candidate.event_key, candidate.cooldown_minutes):
                continue
            signal = Signal(
                portfolio_id=portfolio.id,
                asset_id=candidate.asset_id,
                position_id=candidate.position_id,
                signal_type=candidate.signal_type,
                severity=candidate.severity,
                confidence_score=candidate.confidence_score,
                title=candidate.title,
                message=candidate.message,
                action_idea=candidate.action_idea,
                reasoning=candidate.reasoning,
                risk_note=candidate.risk_note,
                explanation=candidate.explanation,
                event_key=candidate.event_key,
                metrics_json=candidate.metrics_json,
            )
            self.session.add(signal)
            created.append(signal)
        self.session.flush()
        return created

    def _signal_exists(self, portfolio_id: UUID, event_key: str, cooldown_minutes: int) -> bool:
        from app.db.models import utc_now

        cutoff = utc_now() - timedelta(minutes=cooldown_minutes)
        statement = (
            select(Signal.id)
            .where(
                Signal.portfolio_id == portfolio_id,
                Signal.event_key == event_key,
                Signal.created_at >= cutoff,
            )
            .limit(1)
        )
        return self.session.scalar(statement) is not None
