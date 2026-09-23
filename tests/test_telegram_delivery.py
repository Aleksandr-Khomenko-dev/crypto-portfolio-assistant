"""Scanner Telegram delivery: config gates, send/failure paths, dedup and fail-safety.

The Telegram Bot API is mocked at aiogram's Bot.send_message, so everything above it
(routing, dedup, formatting, TelegramService) runs as in production.
"""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from app.config import Settings
from app.db.scanner_models import MarketSetup, ScannerSnapshot
from app.scanner.notifications import DRY_RUN_LABEL, deliver_notifications
from app.services.scanner_service import ScannerRuntime, ScannerService
from app.telegram import scanner_smoke_test, smoke_test
from tests.scanner_fixtures import FixtureFutures
from tests.test_scanner_service import save_ready

TOKEN = "123456:SECRET-TOKEN-must-never-appear"
LIVE = {"telegram_bot_token": TOKEN, "scanner_telegram_chat_id": "777"}


@pytest.fixture()
def telegram_api(monkeypatch):
    """Replaces the HTTP call to the Telegram Bot API; records sent texts."""
    sent = AsyncMock(
        side_effect=lambda *a, **k: SimpleNamespace(message_id=100 + sent.call_count)
    )
    monkeypatch.setattr(Bot, "send_message", sent)
    return sent


def assert_no_token(*texts):
    for text in texts:
        assert TOKEN not in text and "SECRET-TOKEN" not in text


def failure(exc):
    return AsyncMock(side_effect=exc)


# --- configuration gates -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{}, {"telegram_bot_token": TOKEN}, {"scanner_telegram_chat_id": "777"}],
    ids=["disabled", "missing-chat-id", "missing-token"],
)
async def test_missing_telegram_config_sends_nothing(session, telegram_api, overrides):
    settings, _, _, row = save_ready(session, **overrides)
    assert await deliver_notifications(session, settings, datetime.now(UTC)) == 0
    telegram_api.assert_not_called()
    assert row.last_notified_at is None and not row.notified_data


async def test_send_telegram_false_skips_delivery_entirely(session, monkeypatch):
    deliver = AsyncMock()
    monkeypatch.setattr("app.services.scanner_service.deliver_notifications", deliver)
    settings = Settings(scanner_send_telegram=False, **LIVE)
    await ScannerService(session, ScannerRuntime(FixtureFutures(), settings)).run()
    deliver.assert_not_called()


# --- smoke test command ---------------------------------------------------------


async def test_smoke_test_refuses_without_config(capsys):
    assert await smoke_test.run(Settings()) == 2
    out = capsys.readouterr().out
    assert "CPDA_TELEGRAM_BOT_TOKEN" in out and "CPDA_SCANNER_TELEGRAM_CHAT_ID" in out


async def test_smoke_test_success_sends_exactly_one_message(
    capsys, caplog, telegram_api
):
    caplog.set_level(logging.DEBUG)
    assert await smoke_test.run(Settings(**LIVE)) == 0
    telegram_api.assert_called_once()
    kwargs = telegram_api.call_args.kwargs
    assert kwargs["chat_id"] == "777"
    assert "connectivity test" in kwargs["text"] and "No trade signal" in kwargs["text"]
    out = capsys.readouterr().out
    assert "succeeded. message_id=101" in out
    assert_no_token(out, caplog.text)


@pytest.mark.parametrize(
    "exc",
    [
        TelegramBadRequest(SendMessage(chat_id=1, text="x"), "chat not found"),
        RuntimeError(f"https://api.telegram.org/bot{TOKEN}/sendMessage failed"),
    ],
)
async def test_smoke_test_failure_is_reported_without_token(capsys, monkeypatch, exc):
    monkeypatch.setattr(Bot, "send_message", failure(exc))
    assert await smoke_test.run(Settings(**LIVE)) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert_no_token(out)


# --- scanner alert path ---------------------------------------------------------


async def test_ready_alert_sent_once_then_duplicate_suppressed(session, telegram_api):
    settings, _, _, row = save_ready(session, **LIVE)
    now = datetime.now(UTC)
    assert row.readiness == "READY" and row.score >= settings.scanner_alert_score
    assert await deliver_notifications(session, settings, now) == 1
    text = telegram_api.call_args.kwargs["text"]
    assert "AAAUSDT" in text and "READY" in text and "No automatic trading" in text
    assert "DRY RUN" not in text
    assert "Exchange: Binance" in text  # legacy fixture result from Binance
    assert await deliver_notifications(session, settings, now) == 0
    assert telegram_api.call_count == 1
    assert row.notified_data["ready_notified"] is True


async def test_dry_run_alerts_are_labelled(session, telegram_api):
    settings, _, _, _ = save_ready(session, scanner_dry_run=True, **LIVE)
    await deliver_notifications(session, settings, datetime.now(UTC))
    assert telegram_api.call_args.kwargs["text"].startswith(
        "<b>" + DRY_RUN_LABEL.split("\n")[0]
    )


async def test_scanner_path_sequence_matches_production_rules(
    session, telegram_api, capsys
):
    settings = Settings(**LIVE)
    delivered = await scanner_smoke_test.run_sequence(settings, session)
    assert delivered == [True, False, False, True]
    texts = [c.kwargs["text"] for c in telegram_api.call_args_list]
    assert len(texts) == 2
    assert all(t.startswith("<b>🧪 TEST SCANNER ALERT") for t in texts)
    assert "ARBUSDT" in texts[0] and "Score: 85 / 100" in texts[0]
    assert "NEXT: READY" in texts[0] and "HIGH_CONFLUENCE LONG" in texts[0]
    assert "INVALIDATED" in texts[1]
    assert all("Exchange: BingX" in t for t in texts)  # default provider
    out = capsys.readouterr().out
    assert out.count("SUPPRESSED") == 2 and out.count("-> SENT") == 2
    assert_no_token(out)


async def test_scanner_smoke_command_refuses_without_config(capsys, monkeypatch):
    monkeypatch.setattr(scanner_smoke_test, "get_settings", Settings)
    assert await scanner_smoke_test.main_async() == 2
    assert "CPDA_SCANNER_TELEGRAM_CHAT_ID" in capsys.readouterr().out


# --- failure handling -----------------------------------------------------------


async def test_failed_send_is_recorded_and_retried_later(session, monkeypatch):
    settings, _, _, row = save_ready(session, **LIVE)
    monkeypatch.setattr(Bot, "send_message", failure(RuntimeError("network down")))
    assert await deliver_notifications(session, settings, datetime.now(UTC)) == 0
    assert row.delivery_error == "RuntimeError" and row.last_notified_at is None
    assert row.notified_data["failures"] == 1


async def test_timeout_is_bounded_and_recorded(session, monkeypatch):
    settings, _, _, row = save_ready(session, http_timeout_seconds=0.05, **LIVE)

    async def hang(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(Bot, "send_message", hang)
    assert await deliver_notifications(session, settings, datetime.now(UTC)) == 0
    assert row.delivery_error == "TimeoutError"


async def test_flood_limit_429_defers_and_stops_batch(session, monkeypatch):
    settings, repo, result, _ = save_ready(session, **LIVE)
    second = result.model_copy(deep=True)
    second.symbol = "BBBUSDT"
    repo.save(session.scalar(select(ScannerSnapshot.run_id)), second, settings)
    session.commit()
    retry = TelegramRetryAfter(
        SendMessage(chat_id=1, text="x"), "Too Many Requests", 30
    )
    sender = failure(retry)
    monkeypatch.setattr(Bot, "send_message", sender)
    now = datetime.now(UTC)
    assert await deliver_notifications(session, settings, now) == 0
    assert sender.call_count == 1  # the rest of the batch waits for the flood limit
    rows = session.scalars(select(MarketSetup)).all()
    assert len(rows) == 2 and all("retry_at" in r.notified_data for r in rows)
    # Within the Retry-After window nothing is attempted again.
    assert await deliver_notifications(session, settings, now) == 0
    assert sender.call_count == 1


async def test_scan_survives_telegram_failure_with_data_persisted(session, monkeypatch):
    monkeypatch.setattr(
        "app.services.scanner_service.deliver_notifications",
        AsyncMock(side_effect=RuntimeError("telegram exploded")),
    )
    settings = Settings(**LIVE)
    run = await ScannerService(
        session, ScannerRuntime(FixtureFutures(), settings)
    ).run()
    assert run.status == "COMPLETED" and run.analyzed == 2
    assert session.scalar(select(func.count()).select_from(ScannerSnapshot)) == 2
    assert "outcomes" not in run.errors


async def test_scheduler_job_survives_scanner_exception(monkeypatch, caplog):
    from app.scheduler import jobs

    class ExplodingService:
        def __init__(self, *args):
            pass

        async def run(self):
            raise RuntimeError(TOKEN)

    monkeypatch.setattr("app.services.scanner_service.ScannerService", ExplodingService)
    monkeypatch.setattr("app.api.deps.get_scanner_runtime", lambda: None)
    monkeypatch.setattr(jobs, "get_db_session", lambda: _NullSession())
    await jobs.run_scanner_job()  # must not raise
    assert "Scanner job failed error=RuntimeError" in caplog.text
    assert_no_token(caplog.text)


class _NullSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
