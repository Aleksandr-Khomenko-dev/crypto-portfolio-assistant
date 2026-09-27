from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.scanner_models import MarketSetup, ScannerSnapshot
from app.scanner.domain import ScannerResult, Setup, SignalState
from app.scanner.repository import utc
from app.services.telegram_service import TelegramService
from app.telegram.scanner import Mode, format_setup

logger = logging.getLogger(__name__)
STATE_RANK = {state.value: rank for rank, state in enumerate(SignalState)}


def should_notify(setup: MarketSetup, now: datetime, settings: Settings) -> bool:
    old = setup.notified_data or {}
    retry_at = old.get("retry_at")
    if retry_at and now < datetime.fromisoformat(retry_at):
        return False
    if setup.lifecycle != "ACTIVE":
        return "score" in old and old.get("lifecycle") != setup.lifecycle
    if (
        setup.score < settings.scanner_alert_score
        or setup.readiness != "READY"
        or utc(setup.expires_at) <= now
    ):
        return False
    # Only one alert per closed 1h candle, even if live derivatives change its score.
    snapshot_close = (setup.data or {}).get("signal_candle_closed_at")
    if snapshot_close and old.get("signal_candle_closed_at") == snapshot_close:
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


def _news_context(session: Session, setup: MarketSetup, as_of: datetime) -> list:
    """Up to 3 relevant news events RECEIVED by `as_of` (the scan time), recorded as
    setup links for research. News is context only; failures never block the alert."""
    try:
        from app.news.domain import EntityMatch, MatchType
        from app.news.repository import ActiveSetupRef, link_setup, setup_news_context

        contexts = setup_news_context(session, setup.symbol, as_of)
        session.commit()
        ref = ActiveSetupRef(
            setup_id=setup.id,
            exchange=setup.exchange,
            symbol=setup.symbol,
            direction=setup.direction,
            score=setup.score,
            state=setup.state,
            readiness=setup.readiness,
            created_at=utc(setup.created_at),
            telegram_message_id=None,
        )
        for context in contexts:
            match = EntityMatch(
                symbol=context.matched_symbol,
                match_type=MatchType(context.match_type),
                confidence=1.0,
                matched_text=context.matched_symbol,
            )
            link_setup(
                session, context.cluster_id, ref, match, "setup_alert_context", as_of
            )
        return contexts
    except Exception as exc:  # noqa: BLE001 - news must never block a scanner alert
        session.rollback()
        logger.warning("News context unavailable error=%s", type(exc).__name__)
        return []


async def deliver_notifications(
    session: Session,
    settings: Settings,
    now: datetime,
    mode: Mode | None = None,
    limit: int | None = None,
) -> int:
    """Send due scanner alerts; returns how many Telegram accepted.

    `mode` labels every message (TEST for integration checks; DRY_RUN is implied by
    CPDA_SCANNER_DRY_RUN). Which alerts are due is decided by should_notify only.
    `limit` is the remaining per-run budget when a scan delivers in several passes;
    callers must serialise calls (ScannerRuntime.notify_lock) so no alert is sent twice.
    """
    telegram = TelegramService(settings)
    if not telegram.enabled or not settings.scanner_telegram_chat_id:
        logger.info(
            "Scanner Telegram delivery skipped: CPDA_TELEGRAM_BOT_TOKEN or "
            "CPDA_SCANNER_TELEGRAM_CHAT_ID is not set"
        )
        return 0
    if mode is None:
        mode = "DRY_RUN" if settings.scanner_dry_run else "LIVE"
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
        news = (
            _news_context(session, setup, now)
            if setup.lifecycle == "ACTIVE" and settings.news_enabled
            else []
        )
        message = format_setup(
            Setup.model_validate(setup.data),
            result,
            setup.lifecycle,
            mode=mode,
            language=settings.telegram_language,
            episode_invalidation=setup.initial_invalidation,
            news=news,
        )
        pending.append((setup.id, message))
        if len(pending) >= min(settings.scanner_alerts_per_run, limit or 10**9):
            break
    session.commit()  # Release reads before waiting for Telegram or its pacing delay.
    accepted = 0
    for index, (setup_id, message) in enumerate(pending):
        if index:
            await asyncio.sleep(settings.scanner_telegram_interval_seconds)
        sent, error, retry, message_id = False, None, None, None
        try:
            # Bounded wait: a hung request must not hold the scan lock indefinitely.
            response = await asyncio.wait_for(
                telegram.send_scanner(settings.scanner_telegram_chat_id, message),
                timeout=settings.http_timeout_seconds,
            )
            sent = bool(response)
            if isinstance(response, list) and response:
                message_id = response[0]
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
            accepted += 1
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
                "signal_candle_closed_at": (setup.data or {}).get("signal_candle_closed_at"),
                # The first alert's message: news follow-ups reply under it.
                "telegram_message_id": old.get("telegram_message_id") or message_id,
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
        logger.info(
            "Scanner Telegram alert symbol=%s direction=%s lifecycle=%s sent=%s error=%s",
            setup.symbol,
            setup.direction,
            setup.lifecycle,
            sent,
            error,
        )
        if retry is not None:
            # A flood limit applies to the whole bot/chat: defer the rest of the batch
            # too, so the next cycle cannot immediately hit the limit again.
            retry_at = setup.notified_data["retry_at"]
            for later_id, _ in pending[index + 1 :]:
                later = session.get(MarketSetup, later_id)
                if later is not None:
                    later.notified_data = {
                        **(later.notified_data or {}),
                        "retry_at": retry_at,
                    }
            session.commit()
            break
    return accepted
