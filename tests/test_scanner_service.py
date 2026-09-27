from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.api.deps import get_scanner_runtime
from app.config import Settings, get_settings
from app.db.scanner_models import MarketSetup, ScannerRun, ScannerSnapshot
from app.scanner.domain import Derivatives, Direction, MarketContext, Ticker
from app.scanner.notifications import deliver_notifications, should_notify
from app.scanner.repository import ScannerRepository
from app.scanner.scoring import build_result
from app.services.scanner_service import (
    ScannerBusyError,
    ScannerRuntime,
    ScannerService,
)
from app.telegram.scanner import format_ranking, format_setup
from tests.scanner_fixtures import FixtureFutures
from tests.test_scanner_scoring import aligned_frames


def save_ready(session, now=None, **setting_overrides):
    now = now or datetime.now(UTC)
    settings = Settings(**setting_overrides)
    frames = aligned_frames()
    data = Derivatives(
        funding_state="NEUTRAL", interpretation="NEW_LONG_PARTICIPATION_POSSIBLE"
    )
    result = build_result(
        "AAAUSDT",
        frames,
        data,
        MarketContext(state="BULLISH"),
        Ticker(symbol="AAAUSDT", price=112, quote_volume=20_000_000, timestamp=now),
        now,
        settings,
    )
    run = ScannerRun(started_at=now)
    session.add(run)
    session.commit()
    repo = ScannerRepository(session)
    repo.save(run.id, result, settings)
    session.commit()
    row = session.scalar(select(MarketSetup).where(MarketSetup.direction == "LONG"))
    assert row is not None
    return settings, repo, result, row


async def test_scanner_failure_isolation_snapshots_context_and_busy(session):
    runtime = ScannerRuntime(
        FixtureFutures(fail={"BBBUSDT"}, derivatives_fail=True), Settings()
    )
    result = await ScannerService(session, runtime).run()
    assert result.status == "PARTIAL" and result.analyzed == 1 and result.failed == 1
    snapshots = list(session.scalars(select(ScannerSnapshot)))
    assert len(snapshots) == 1
    assert snapshots[0].data["unavailable"] == ["on-chain", "macro", "news/fundamental"]
    assert snapshots[0].data["derivatives"]["errors"]
    assert snapshots[0].data["context"]["assets"]["BTCUSDT"]
    async with runtime.lock:
        with pytest.raises(ScannerBusyError):
            await ScannerService(session, runtime).run()
    await runtime.aclose()
    assert runtime.provider.closed


async def test_universe_failure_is_recorded_and_lock_released(session):
    provider = FixtureFutures()
    provider.contracts = AsyncMock(side_effect=RuntimeError("unavailable"))
    runtime = ScannerRuntime(provider, Settings())
    run = await ScannerService(session, runtime).run()
    assert run.status == "FAILED" and run.errors == {"run": "RuntimeError"}
    assert not runtime.lock.locked()
    runtime.settings.scanner_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await ScannerService(session, runtime).run()


def test_setup_dedup_durable_cooldown_ready_expiry_and_invalidation(session):
    now = datetime.now(UTC)
    settings, repo, result, row = save_ready(session, now)
    assert should_notify(row, now, settings)
    row.notified_data = {
        "state": row.state,
        "score": row.score,
        "readiness": "WAIT_RETEST",
        "lifecycle": "ACTIVE",
    }
    row.last_notified_at = now
    assert should_notify(
        row, now, settings
    )  # READY transition bypasses normal cooldown.
    row.notified_data = {**row.notified_data, "readiness": row.readiness}
    assert not should_notify(row, now + timedelta(hours=3), settings)
    row.score += settings.scanner_score_delta
    assert not should_notify(row, now + timedelta(minutes=1), settings)
    assert should_notify(row, now + timedelta(hours=2), settings)
    old_expiry = row.expires_at
    result.created_at += timedelta(minutes=5)
    run_id = session.scalar(select(ScannerRun.id))
    repo.save(run_id, result, settings)
    session.commit()
    assert session.scalar(select(func.count()).select_from(MarketSetup)) == 1
    assert row.expires_at == old_expiry
    # Only a candle closing after episode creation can invalidate it.
    result.price = row.initial_invalidation - 1
    result.candle_closed_at = result.created_at + timedelta(minutes=1)
    result.created_at += timedelta(minutes=15)
    repo.save(run_id, result, settings)
    session.commit()
    assert row.lifecycle == "INVALIDATED"
    assert should_notify(row, now, settings)
    assert repo.setups(now=now) == []
    row.lifecycle = "ACTIVE"
    row.expires_at = now
    session.commit()
    repo.expire(now + timedelta(seconds=1))
    session.commit()
    assert row.lifecycle == "EXPIRED"
    assert should_notify(row, now, settings)



def test_public_alert_requires_ready_and_new_closed_hourly_candle(session):
    now = datetime.now(UTC)
    settings, _, result, row = save_ready(session, now)
    row.readiness = "WAIT_RETEST"
    row.score = 95
    assert not should_notify(row, now, settings)
    row.readiness = "READY"
    assert should_notify(row, now, settings)
    row.notified_data = {
        "score": row.score,
        "state": row.state,
        "readiness": "READY",
        "lifecycle": "ACTIVE",
        "signal_candle_closed_at": result.candle_closed_at.isoformat(),
    }
    row.last_notified_at = now
    row.score = 100
    assert not should_notify(row, now + timedelta(hours=2), settings)


def test_legacy_15m_episode_is_retired_on_first_1h_scan(session):
    now = datetime.now(UTC)
    settings, repo, result, old = save_ready(session, now)
    old_snapshot = session.get(ScannerSnapshot, old.snapshot_id)
    old_snapshot.data = {**old_snapshot.data, "signal_timeframe": "15m"}
    session.commit()
    result.created_at += timedelta(minutes=5)
    repo.save(session.scalar(select(ScannerRun.id)), result, settings)
    session.commit()
    assert old.lifecycle == "EXPIRED"
    assert not should_notify(old, result.created_at, settings)
    active = session.scalars(select(MarketSetup).where(MarketSetup.lifecycle == "ACTIVE")).all()
    assert len(active) == 1 and active[0].id != old.id


async def test_notification_success_failure_and_html_safety(session, monkeypatch):
    settings, repo, result, row = save_ready(
        session, scanner_telegram_chat_id="123", telegram_bot_token="123456:secret"
    )
    now = datetime.now(UTC)
    sender = AsyncMock(side_effect=RuntimeError("secret must never persist"))
    monkeypatch.setattr(
        "app.services.telegram_service.TelegramService.send_scanner", sender
    )
    await deliver_notifications(session, settings, now)
    assert row.last_notified_at is None and row.delivery_error == "RuntimeError"
    sender.side_effect = None
    sender.return_value = True
    await deliver_notifications(session, settings, now)
    assert row.last_notified_at is not None and row.delivery_error is None
    count = sender.call_count
    await deliver_notifications(session, settings, now)
    assert sender.call_count == count
    # A permanently rejected message backs off instead of retrying every cycle.
    row.notified_data, row.last_notified_at = {}, None
    session.commit()
    sender.side_effect = RuntimeError("bad request")
    for _ in range(3):
        await deliver_notifications(session, settings, now)
    assert sender.call_count == count + 3
    assert row.notified_data["failures"] == 3 and "retry_at" in row.notified_data
    await deliver_notifications(session, settings, now + timedelta(seconds=30))
    assert sender.call_count == count + 3
    sender.side_effect = None
    await deliver_notifications(session, settings, now + timedelta(minutes=2))
    assert sender.call_count == count + 4 and "failures" not in row.notified_data
    text = format_setup(
        result.setups[0].model_copy(update={"symbol": "<unsafe>"}), result
    )
    assert "&lt;unsafe&gt;" in text and "<unsafe>" not in text
    assert "а не вероятность успешной сделки" in text
    assert "Автоматической торговли нет" in text
    assert "AAAUSDT" in format_ranking(repo.setups(now=now), Direction.LONG)
    assert "Сейчас нет подходящих сетапов" in format_ranking([], Direction.SHORT)


def test_scanner_api_run_filters_status_and_detail(client):
    settings = Settings(
        scanner_watch_score=1,
        scanner_setup_score=70,
        scanner_high_score=80,
        scanner_extreme_score=90,
        scanner_use_btc_context=False,
    )
    runtime = ScannerRuntime(FixtureFutures(), settings)
    client.app.dependency_overrides[get_scanner_runtime] = lambda: runtime
    client.app.dependency_overrides[get_settings] = lambda: settings
    try:
        assert client.get("/api/scanner/status").json()["last_run"] is None
        assert client.get("/api/scanner/setups/MISSING").status_code == 404
        response = client.post("/api/scanner/run")
        assert response.status_code == 200, response.text
        assert response.json()["analyzed"] == 2
        assert (
            client.get("/api/scanner/status").json()["last_run"]["status"]
            == "COMPLETED"
        )
        assert len(client.get("/api/scanner/snapshots").json()) == 2
        detail = client.get("/api/scanner/setups/aaausdt")
        assert detail.status_code == 200 and len(detail.json()["setups"]) == 2
        rows = client.get(
            "/api/scanner/setups?direction=LONG&symbol=AAAUSDT&minimum_score=1"
        ).json()
        assert len(rows) == 1 and rows[0]["direction"] == "LONG"
        for path in ("/api/scanner/top/long", "/api/scanner/top/short"):
            scores = [r["score"] for r in client.get(path).json()]
            assert scores == sorted(scores, reverse=True)
        assert client.get("/api/scanner/top/nope").status_code == 422
        assert client.get("/api/scanner/setups?minimum_score=101").status_code == 422
        settings.scanner_enabled = False
        assert client.post("/api/scanner/run").status_code == 409
        assert "Market Scanner" in client.get("/").text
    finally:
        client.app.dependency_overrides.clear()


def test_tradingview_disabled_authenticated_validated_idempotent(client, session):
    payload = {
        "event_id": "event-1",
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "event": "BOS",
        "direction": "LONG",
        "price": "100",
        "timestamp": datetime.now(UTC).isoformat(),
        "secret": "x" * 24,
    }
    assert client.post("/api/webhooks/tradingview", json=payload).status_code == 404
    settings = Settings(
        tradingview_webhook_enabled=True, tradingview_webhook_secret="x" * 24
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    try:
        assert (
            client.post(
                "/api/webhooks/tradingview", json={**payload, "secret": "wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/webhooks/tradingview", json={**payload, "price": "NaN"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/webhooks/tradingview",
                json={**payload, "timestamp": "2020-01-01T00:00:00Z"},
            ).status_code
            == 422
        )
        first = client.post("/api/webhooks/tradingview", json=payload)
        assert first.status_code == 200 and not first.json()["used_in_scoring"]
        assert client.post("/api/webhooks/tradingview", json=payload).json()[
            "duplicate"
        ]
        from app.db.scanner_models import TradingViewEvent

        assert "secret" not in session.get(TradingViewEvent, "event-1").data
    finally:
        client.app.dependency_overrides.clear()


async def test_scanner_telegram_commands_and_scheduler(session):
    from unittest.mock import Mock

    from aiogram.filters import CommandObject

    from app.scheduler.jobs import build_scheduler
    from app.telegram.scanner_handlers import scanner_handler

    save_ready(session)
    for name in ("scanner", "toplong", "topshort", "watchlist", "setup"):
        message = Mock()
        message.answer = AsyncMock()
        await scanner_handler(
            message,
            CommandObject(
                prefix="/", command=name, args="AAAUSDT" if name == "setup" else None
            ),
        )
        assert message.answer.called
    scheduler = build_scheduler(Settings())
    job = scheduler.get_job("market-scanner")
    assert job is not None and job.max_instances == 1 and job.coalesce
    assert (
        build_scheduler(Settings(scanner_enabled=False)).get_job("market-scanner")
        is None
    )
