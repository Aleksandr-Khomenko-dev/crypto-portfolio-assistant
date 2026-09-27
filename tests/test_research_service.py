"""Outcome persistence, processing, API, Telegram and dry-run smoke tests."""

from collections import Counter
from datetime import UTC, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.api.deps import get_scanner_runtime
from app.config import Settings, get_settings
from app.db.scanner_models import ScannerRun, SetupOutcome
from app.db.session import get_session_factory
from app.research.outcomes import QUANTUM
from app.scanner.domain import Candle
from app.services.outcome_service import OutcomeService
from app.services.scanner_service import ScannerRuntime, ScannerService
from tests.scanner_fixtures import FixtureFutures
from tests.test_scanner_service import save_ready


class BarsProvider:
    """Serves prepared closed candles; counts calls so reuse can be asserted."""

    exchange = "BINANCE"

    def __init__(self, bars, children=None):
        self.bars, self.children = bars, children or []
        self.calls = Counter()

    async def candles(self, symbol, timeframe, now):
        self.calls["candles"] += 1
        return self.bars

    async def range_candles(self, symbol, timeframe, start, end):
        self.calls["range_candles"] += 1
        return [
            c for c in self.children if c.open_time >= start and c.close_time <= end
        ]


def ready_outcome(session, **settings):
    settings, repo, result, row = save_ready(session, **settings)
    outcome = session.scalar(select(SetupOutcome))
    assert outcome is not None
    return settings, repo, result, row, outcome


def candle(start, minutes, high, low):
    mid = (high + low) / 2
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=minutes, milliseconds=-1),
        open=mid,
        high=high,
        low=low,
        close=mid,
        volume=1,
    )


def after_ready(result, *ranges_in_r):
    """15m candles following the READY candle, given (high_R, low_R) multiples."""
    setup = result.setups[0]
    entry, risk = result.price, result.price - setup.risk.invalidation
    start = result.candle_closed_at + timedelta(milliseconds=1)
    return [
        candle(
            start + timedelta(minutes=15 * i),
            15,
            entry + Decimal(str(h)) * risk,
            entry + Decimal(str(lo)) * risk,
        )
        for i, (h, lo) in enumerate(ranges_in_r)
    ]


def test_outcome_starts_at_first_ready_closed_candle_with_frozen_targets(session):
    settings, repo, result, row, outcome = ready_outcome(session)
    setup = result.setups[0]
    entry = result.price.quantize(QUANTUM)
    risk = entry - setup.risk.invalidation.quantize(QUANTUM)
    assert outcome.market_setup_id == row.id
    assert outcome.ready_at.replace(tzinfo=UTC) == result.candle_closed_at
    assert outcome.entry_reference_price == entry
    assert outcome.initial_risk_distance == risk > 0
    assert outcome.target_1r_price == entry + risk
    assert outcome.target_2r_price == entry + 2 * risk
    assert outcome.score_at_entry == setup.score
    assert outcome.score_breakdown == setup.blocks
    assert (outcome.market_regime, outcome.structure_regime) == ("BULLISH", "TREND")
    assert outcome.outcome_status == "TRACKING" and outcome.bars_processed == 0
    # SHORT is not READY, so it has no outcome.
    assert session.scalar(select(func.count()).select_from(SetupOutcome)) == 1

    # A later READY evaluation of the same episode, with changed structure and a new
    # closed candle, must not start a second outcome or move the frozen levels.
    later = result.model_copy(deep=True)
    later.created_at += timedelta(minutes=15)
    later.candle_closed_at += timedelta(minutes=15)
    later.price += 1
    later.setups[0].risk.invalidation -= 1
    repo.save(session.scalar(select(ScannerRun.id)), later, settings)
    session.commit()
    session.refresh(outcome)
    assert session.scalar(select(func.count()).select_from(SetupOutcome)) == 1
    assert outcome.entry_reference_price == entry
    assert outcome.target_1r_price == entry + risk


async def test_processing_restart_persistence_idempotency_and_no_future_bars(
    session, sqlite_database_url
):
    settings, _, result, _, outcome = ready_outcome(session)
    bars = after_ready(result, (0.6, -0.2), (1.1, 0.3), (0.8, -1.05))
    provider = BarsProvider(bars)

    # Only the first candle has closed at this `now`; later ones must not be used.
    stats = await OutcomeService(session, provider, settings).process(
        bars[0].close_time
    )
    session.refresh(outcome)
    assert stats["outcomes_updated"] == 1 and outcome.bars_processed == 1
    assert outcome.first_0_5r == "TARGET_FIRST" and outcome.first_1r == "PENDING"

    # Restart: a new session and provider continue from the persisted cursor.
    later = bars[-1].close_time + timedelta(minutes=1)
    with get_session_factory(sqlite_database_url)() as restarted:
        stats = await OutcomeService(restarted, BarsProvider(bars), settings).process(
            later
        )
        assert stats["outcomes_completed"] == 1
        again = await OutcomeService(restarted, BarsProvider(bars), settings).process(
            later
        )
        assert again["outcomes_updated"] == 0
    session.refresh(outcome)
    assert outcome.outcome_status == "INVALIDATED" and outcome.bars_processed == 3
    assert (outcome.first_1r, outcome.first_1_5r) == (
        "TARGET_FIRST",
        "INVALIDATION_FIRST",
    )
    assert outcome.bars_to_1r == 2 and outcome.bars_to_invalidation == 3
    assert outcome.max_favorable_excursion_r == pytest.approx(1.1)
    assert outcome.max_adverse_excursion_r == pytest.approx(-1.05)
    assert outcome.completed_at is not None


async def test_scanner_bars_are_reused_instead_of_refetched(session):
    settings, _, result, _, outcome = ready_outcome(session)
    bars = after_ready(result, (0.2, -0.2))
    provider = BarsProvider([])
    await OutcomeService(session, provider, settings).process(
        bars[0].close_time, {("BINANCE", "AAAUSDT", "15m"): bars}
    )
    session.refresh(outcome)
    assert provider.calls["candles"] == 0 and outcome.bars_processed == 1


async def test_same_bar_ambiguity_uses_lower_timeframe_when_available(session):
    settings, _, result, _, outcome = ready_outcome(session)
    [parent] = after_ready(result, (1.2, -1.2))
    entry = result.price
    risk = entry - result.setups[0].risk.invalidation
    kids = [
        candle(
            parent.open_time + timedelta(minutes=5 * i),
            5,
            entry + h * risk,
            entry + lo * risk,
        )
        for i, (h, lo) in enumerate(
            [
                (Decimal("0.6"), Decimal("-0.1")),
                (Decimal("1.2"), Decimal(0)),
                (Decimal("0.5"), Decimal("-1.2")),
            ]
        )
    ]
    provider = BarsProvider([parent], kids)
    stats = await OutcomeService(session, provider, settings).process(parent.close_time)
    session.refresh(outcome)
    assert stats["ambiguity_ltf_resolved"] == 1
    assert (
        outcome.first_1r == "TARGET_FIRST"
        and outcome.first_1_5r == "INVALIDATION_FIRST"
    )
    assert outcome.ambiguity_resolution == "LOWER_TIMEFRAME"
    assert outcome.outcome_status == "INVALIDATED"


async def test_same_bar_ambiguity_is_reported_when_resolution_disabled(session):
    settings, _, result, _, outcome = ready_outcome(
        session, outcome_ltf_resolution=False
    )
    [parent] = after_ready(result, (1.2, -1.2))
    provider = BarsProvider([parent], [])
    await OutcomeService(session, provider, settings).process(parent.close_time)
    session.refresh(outcome)
    assert provider.calls["range_candles"] == 0
    assert outcome.outcome_status == "AMBIGUOUS_SAME_BAR"
    assert outcome.first_1r == "AMBIGUOUS" and outcome.hit_minus_1r


async def test_research_api_filters_and_reports(client, session):
    settings, _, result, row, outcome = ready_outcome(session)
    bars = after_ready(result, (2.2, 0.1))
    await OutcomeService(session, BarsProvider(bars), settings).process(
        bars[0].close_time
    )
    outcomes = client.get("/api/research/outcomes?symbol=aaa&direction=LONG").json()
    assert len(outcomes) == 1 and outcomes[0]["outcome_status"] == "COMPLETED_2R"
    assert outcomes[0]["symbol"] == "AAAUSDT"
    assert client.get("/api/research/outcomes?direction=SHORT").json() == []
    assert client.get("/api/research/outcomes?status=TRACKING").json() == []
    assert client.get("/api/research/outcomes?regime=trend").json()
    assert client.get("/api/research/outcomes?score_max=10").json() == []
    assert client.get("/api/research/outcomes?status=WON").status_code == 422

    report = client.get("/api/research/calibration").json()
    assert "not probabilities" in report["note"].lower()
    band = next(b for g in report["groups"] for b in g["bands"] if b["sample_count"])
    assert band["band"] == "90–100" and band["low_sample"]
    assert band["targets"][1] == {
        "target": "+1R",
        "target_first": 1,
        "invalidation_first": 0,
        "resolved": 1,
        "ambiguous": 0,
        "unresolved": 0,
        "observed_rate": 1.0,
    }
    grouped = client.get("/api/research/calibration?group_by=direction,asset_class")
    assert grouped.json()["groups"][0]["key"] == {
        "direction": "LONG",
        "asset_class": "ALT",
    }
    assert client.get("/api/research/calibration?group_by=bogus").status_code == 422
    other = client.get("/api/research/calibration/BTCUSDT").json()
    assert other["groups"] == [] and other["filters"] == {"symbol": "BTCUSDT"}
    assert client.get("/api/research/calibration/AAAUSDT").json()["groups"]

    detail = client.get(f"/api/research/setup/{row.id}/outcome")
    assert detail.status_code == 200 and detail.json()["id"] == str(outcome.id)
    missing = client.get(f"/api/research/setup/{outcome.id}/outcome")
    assert missing.status_code == 404


async def test_telegram_research_commands(session):
    from aiogram.filters import CommandObject

    from app.telegram.scanner_handlers import research_handler

    settings, _, result, _, _ = ready_outcome(session)
    bars = after_ready(result, (0.3, -1.1))
    await OutcomeService(session, BarsProvider(bars), settings).process(
        bars[0].close_time
    )
    texts = []
    for command, args in (
        ("performance", None),
        ("performance", "aaa"),
        ("calibration", None),
    ):
        message = Mock()
        message.answer = AsyncMock()
        await research_handler(
            message, CommandObject(prefix="/", command=command, args=args)
        )
        texts.append(message.answer.call_args.args[0])
    assert "Samples: 1" in texts[0] and "small sample" in texts[0]
    assert "+1R first: 0% (resolved 1)" in texts[0]
    assert "not a probability" in texts[0]
    assert "AAAUSDT" in texts[1] and "LONG" in texts[2]
    message = Mock()
    message.answer = AsyncMock()
    await research_handler(
        message, CommandObject(prefix="/", command="performance", args="$$")
    )
    assert "Usage" in message.answer.call_args.args[0]


async def test_dry_run_scan_persists_telemetry_and_suppresses_telegram(
    session, client, monkeypatch
):
    deliver = AsyncMock(return_value=0)
    monkeypatch.setattr("app.services.scanner_service.deliver_notifications", deliver)
    settings = Settings(
        scanner_dry_run=True,
        scanner_send_telegram=False,
        scanner_telegram_chat_id="1",
        telegram_bot_token="123:abc",
    )
    runtime = ScannerRuntime(FixtureFutures(), settings)
    run = await ScannerService(session, runtime).run()
    assert run.status == "COMPLETED" and run.analyzed == 2
    deliver.assert_not_called()
    assert run.telemetry["dry_run"] is True
    for key in (
        "markets_requested",
        "markets_analyzed",
        "markets_failed",
        "http_429",
        "http_418",
        "cache_hits",
        "cache_misses",
        "stale_data_skips",
        "high_confluence",
        "ready_setups",
    ):
        assert key in run.telemetry
    assert run.telemetry["markets_requested"] == 2
    client.app.dependency_overrides[get_scanner_runtime] = lambda: runtime
    client.app.dependency_overrides[get_settings] = lambda: settings
    try:
        status = client.get("/api/scanner/status").json()
        assert status["last_run"]["telemetry"]["dry_run"] is True
        assert client.get("/api/research/calibration").status_code == 200
    finally:
        client.app.dependency_overrides.clear()
    # With delivery enabled again, notifications run as before.
    settings.scanner_send_telegram = True
    await ScannerService(session, runtime).run()
    # Delivery now runs as each symbol is persisted, plus a final pass.
    assert deliver.call_count >= 1


async def test_dry_run_runner_once_smoke(sqlite_database_url, monkeypatch):
    from app.scanner import runner

    runtime = ScannerRuntime(FixtureFutures(), Settings(scanner_dry_run=True))
    monkeypatch.setattr(runner, "get_scanner_runtime", lambda: runtime)
    await runner.main(once=True)
    with get_session_factory(sqlite_database_url)() as db:
        run = db.scalar(select(ScannerRun))
        assert run.status == "COMPLETED" and run.telemetry["dry_run"] is True
