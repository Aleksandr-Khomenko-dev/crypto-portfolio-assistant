from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import AlertChannel, AlertEvent, AlertStatus, Portfolio, Signal


class AlertService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_events(
        self,
        portfolio: Portfolio,
        signals: list[Signal],
        *,
        channel: AlertChannel,
        destination: str | None,
    ) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for signal in signals:
            event = AlertEvent(
                signal_id=signal.id,
                portfolio_id=portfolio.id,
                channel=channel,
                destination=destination,
                status=AlertStatus.PENDING,
                dedupe_key=signal.event_key,
            )
            self.session.add(event)
            events.append(event)
        self.session.flush()
        return events

    def mark_sent(self, events: list[AlertEvent]) -> None:
        for event in events:
            event.status = AlertStatus.SENT
            event.delivered_at = datetime.now(timezone.utc)
            self.session.add(event)
        self.session.flush()

    def mark_failed(self, events: list[AlertEvent], error_message: str) -> None:
        for event in events:
            event.status = AlertStatus.FAILED
            event.error_message = error_message
            self.session.add(event)
        self.session.flush()
