from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.scanner_models import MarketSetup, ScannerSnapshot
from app.scanner.domain import ScannerResult, Setup, SignalState
from app.scanner.repository import utc
from app.services.telegram_service import TelegramService
from app.telegram.scanner import format_setup

logger = logging.getLogger(__name__)
STATE_RANK = {state.value: rank for rank, state in enumerate(SignalState)}


def should_notify(setup: MarketSetup, now: datetime, settings: Settings) -> bool:
    old = setup.notified_data or {}
    retry_at = old.get("retry_at")
    if retry_at and now < datetime.fromisoformat(retry_at):
        return False
    if setup.lifecycle != "ACTIVE":
        return "score" in old and old.get("lifecycle") != setup.lifecycle
    if setup.score < settings.scanner_alert_score or utc(setup.expires_at) <= now:
        return False
    ready_sent = old.get("ready_notified", old.get("readiness") == "READY")
    if "score" in old and setup.readiness == "READY" and not ready_sent:
        return True
    if setup.last_notified_at and now < utc(setup.last_notified_at) + timedelta(
        minutes=settings.scanner_cooldown_minutes
    ):
        return False
    if "score" not in old:
        return True
    return (
        STATE_RANK[setup.state] > STATE_RANK[old.get("peak_state", old["state"])]
    ) or (
        setup.score
        >= old.get("peak_score", old["score"]) + settings.scanner_score_delta
    )


async def deliver_notifications(
    session: Session, settings: Settings, now: datetime
) -> None:
    telegram = TelegramService(settings)
    if not telegram.enabled or not settings.scanner_telegram_chat_id:
        return
    rows = list(
        session.scalars(
            select(MarketSetup)
            .where(
                (MarketSetup.lifecycle == "ACTIVE")
                | (MarketSetup.updated_at >= now - timedelta(days=1))
            )
            .order_by(
                MarketSetup.lifecycle.desc(),
                MarketSetup.score.desc(),
                MarketSetup.created_at,
            )
        )
    )
    pending = []
    for setup in rows:
        if not should_notify(setup, now, settings):
            continue
        snapshot = session.get(ScannerSnapshot, setup.snapshot_id)
        if snapshot is None:
            continue
        result = ScannerResult.model_validate(snapshot.data)
        message = format_setup(
            Setup.model_validate(setup.data), result, setup.lifecycle
        )
        if setup.initial_invalidation is not None:
            message += f"\nEpisode invalidation (fixed): {escape(str(setup.initial_invalidation))}"
        pending.append((setup.id, message))
        if len(pending) >= settings.scanner_alerts_per_run:
            break
    session.commit()  # Release reads before waiting for Telegram or its pacing delay.
    for index, (setup_id, message) in enumerate(pending):
        if index:
            await asyncio.sleep(settings.scanner_telegram_interval_seconds)
        sent, error, retry = False, None, None
        try:
            sent = await telegram.send_scanner(
                settings.scanner_telegram_chat_id, message
            )
        except Exception as exc:  # noqa: BLE001 - never log token-bearing exception URLs
            error = type(exc).__name__
            retry = getattr(exc, "retry_after", None)
            logger.warning("Scanner Telegram delivery failed error=%s", error)
        delivered = session.get(MarketSetup, setup_id)
        if delivered is None:
            continue
        setup = delivered
        old = setup.notified_data or {}
        setup.delivery_error = error
        if sent:
            setup.last_notified_at = now
            peak_state = max(
                (setup.state, old.get("peak_state", old.get("state", "IGNORE"))),
                key=STATE_RANK.__getitem__,
            )
            setup.notified_data = {
                "score": setup.score,
                "state": setup.state,
                "readiness": setup.readiness,
                "lifecycle": setup.lifecycle,
                "peak_score": max(
                    setup.score, old.get("peak_score", old.get("score", 0))
                ),
                "peak_state": peak_state,
                "ready_notified": old.get(
                    "ready_notified", old.get("readiness") == "READY"
                )
                or setup.readiness == "READY",
            }
        else:
            # Back off repeated failures so a permanently rejected message cannot
            # occupy the per-run alert slots (and hit Telegram) every cycle.
            failures = int(old.get("failures", 0)) + 1
            delay = (
                max(1, float(retry))
                if retry is not None
                else 0
                if failures < 3
                else min(60 * 2 ** (failures - 3), 6 * 3600)
            )
            setup.notified_data = {**old, "failures": failures}
            if delay:
                setup.notified_data["retry_at"] = (
                    now + timedelta(seconds=delay)
                ).isoformat()
        session.commit()
        if retry is not None:
            break  # A chat-level flood limit applies to the rest of this batch too.
