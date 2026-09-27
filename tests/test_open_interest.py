"""Self-recorded open interest: alignment, horizon deltas, persistence and scoring gate."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.analytics.derivatives import normalize_derivatives
from app.analytics.open_interest import (
    OIObservation,
    aligned_bucket,
    history_points,
    horizon_changes,
    observation_from,
)
from app.config import Settings
from app.db.scanner_models import OpenInterestSnapshot, ScannerSnapshot
from app.db.session import get_session_factory
from app.scanner.domain import Derivatives
from app.scanner.open_interest_repository import load_history, prune, record
from app.services.oi_collector_service import OICollectorService
from app.services.scanner_service import ScannerRuntime, ScannerService
from tests.scanner_fixtures import FixtureFutures, candles

B = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)  # a 15m close boundary
STEP = timedelta(minutes=15)


def obs(bucket, oi, offset=30):
    return OIObservation(bucket, bucket + timedelta(seconds=offset), Decimal(oi))


# --- pure alignment and deltas ---------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0, B), (90, B), (-150, B), (151, None), (-400, None)],
)
def test_alignment_to_close_boundary(offset, expected):
    assert aligned_bucket(B + timedelta(seconds=offset), 150) == expected


def test_initial_oi_is_unavailable_not_zero():
    changes = horizon_changes({B: Decimal(100)}, B)
    assert changes == {"15m": None, "1h": None, "4h": None}
    assert horizon_changes({}, B) == {"15m": None, "1h": None, "4h": None}


def test_15m_1h_4h_deltas_from_real_points_only():
    points = {
        B: Decimal(110),
        B - STEP: Decimal(100),
        B - timedelta(hours=1): Decimal(88),
        B - timedelta(hours=4): Decimal(55),
    }
    changes = horizon_changes(points, B)
    assert changes["15m"] == pytest.approx(10.0)
    assert changes["1h"] == pytest.approx(25.0)
    assert changes["4h"] == pytest.approx(100.0)
    del points[B - timedelta(hours=1)]
    assert horizon_changes(points, B)["1h"] is None  # no interpolation across gaps


def test_closer_observation_wins_and_repeat_is_noop():
    far, near = obs(B, 100, offset=120), obs(B, 101, offset=10)
    assert [p.contracts for p in history_points({B: far}, near)] == [101]
    assert [p.contracts for p in history_points({B: near}, far)] == [101]
    assert [p.contracts for p in history_points({B: near}, near)] == [101]


def bars_until(boundary, count=20, prices=None):
    """15m closed candles whose last close boundary is `boundary`."""
    series = candles("15m", boundary + timedelta(seconds=1), count=count)
    return (
        series
        if prices is None
        else [
            b.model_copy(
                update={
                    "close": Decimal(p),
                    "open": Decimal(p),
                    "high": Decimal(p) + 1,
                    "low": Decimal(p) - 1,
                }
            )
            for b, p in zip(series, prices, strict=True)
        ]
    )


def derivatives(history, now):
    return Derivatives(
        funding_rate=Decimal("0.0001"),
        funding_timestamp=now,
        open_interest=Decimal(1),
        oi_timestamp=now,
        history=history,
        oi_history_source="SELF_RECORDED",
    )


def test_constructive_oi_needs_two_consecutive_real_points():
    now = B + timedelta(seconds=60)
    bars = bars_until(B, prices=[100] * 18 + [100, 105])
    single = normalize_derivatives(
        derivatives(history_points({}, obs(B, 100)), now), bars, Decimal("0.001"), now
    )
    assert single.interpretation == "unavailable" and single.oi_change_pct is None
    assert single.oi_change_by_horizon == {"15m": None, "1h": None, "4h": None}
    both = history_points({B - STEP: obs(B - STEP, 100)}, obs(B, 110))
    paired = normalize_derivatives(derivatives(both, now), bars, Decimal("0.001"), now)
    assert paired.oi_change_pct == pytest.approx(10.0)
    assert paired.interpretation == "NEW_LONG_PARTICIPATION_POSSIBLE"
    assert paired.oi_change_by_horizon["15m"] == pytest.approx(10.0)


def test_future_bucket_is_never_used_before_its_candle_closes():
    now = B + STEP - timedelta(seconds=60)  # observed just before the next boundary
    live = observation_from(derivatives([], now), 150)
    assert live is not None and live.bucket_at == B + STEP
    result = normalize_derivatives(
        derivatives(history_points({B: obs(B, 100)}, live), now),
        bars_until(B),
        Decimal("0.001"),
        now,
    )
    assert result.oi_change_pct is None  # B+15m candle has not closed yet


# --- repository ------------------------------------------------------------------


def test_idempotent_exchange_specific_writes_and_retention(session):
    now = B + timedelta(hours=1)
    assert record(session, "BINGX", "ARBUSDT", obs(B, 100, 120), now) == "inserted"
    assert record(session, "BINGX", "ARBUSDT", obs(B, 100, 120), now) == "kept"
    assert record(session, "BINGX", "ARBUSDT", obs(B, 99, 200), now) == "kept"
    assert record(session, "BINGX", "ARBUSDT", obs(B, 101, 5), now) == "replaced"
    assert record(session, "BINANCE", "ARBUSDT", obs(B, 555), now) == "inserted"
    record(session, "BINGX", "ARBUSDT", obs(B - timedelta(days=20), 90), now)
    session.commit()
    count = select(func.count()).select_from(OpenInterestSnapshot)
    assert session.scalar(count) == 3
    history = load_history(session, "BINGX", ["ARBUSDT"], B - timedelta(hours=5))
    assert next(iter(history["ARBUSDT"].values())).open_interest == Decimal(101)
    assert prune(session, "BINGX", B - timedelta(days=14)) == 1
    session.commit()
    assert session.scalar(count) == 2  # the Binance row is untouched


# --- scanner integration ---------------------------------------------------------


class SelfRecordingExchange(FixtureFutures):
    """An exchange without published OI history (like BingX)."""

    exchange = "BINGX"
    publishes_oi_history = False

    def __init__(self, oi=Decimal(1000), offset=60):
        super().__init__()
        self.oi, self.offset = oi, offset

    @asynccontextmanager
    async def oi_collection_priority(self):
        yield

    async def oi_mark_prices(self, boundary):
        return {symbol: Decimal(112) for symbol in await self.tickers()}

    async def oi_snapshot(self, symbol, boundary, mark):
        return OIObservation(
            boundary, boundary + timedelta(seconds=self.offset), self.oi, self.oi * mark
        )

    async def funding(self, symbol, now):
        return await self.derivatives(symbol, now)

    async def derivatives(self, symbol, now):
        observed = now.replace(second=0, microsecond=0)
        observed = (
            observed
            - timedelta(minutes=observed.minute % 15)
            + timedelta(seconds=self.offset)
        )
        return Derivatives(
            funding_rate=Decimal("0.0001"),
            funding_timestamp=now,
            open_interest=self.oi,
            open_interest_notional=self.oi * 112,
            oi_timestamp=observed,
        )


async def scan(session, provider, now):
    runtime = ScannerRuntime(provider, Settings())
    return await ScannerService(session, runtime).run(now)


def latest_derivatives(session, symbol="AAAUSDT"):
    row = session.scalars(
        select(ScannerSnapshot)
        .where(ScannerSnapshot.symbol == symbol)
        .order_by(ScannerSnapshot.created_at.desc())
    ).first()
    return row.data["derivatives"], row.data["setups"]


async def test_scan_records_restarts_and_awards_oi_only_with_real_history(
    session, sqlite_database_url
):
    first = B + timedelta(seconds=60)
    collector = OICollectorService(
        ScannerRuntime(SelfRecordingExchange(), Settings()),
        get_session_factory(sqlite_database_url),
        clock=lambda: first,
    )
    assert (await collector.collect(B))["oi_snapshots_inserted"] == 2
    run = await scan(session, SelfRecordingExchange(Decimal(1000)), first)
    assert "oi_snapshots_inserted" not in run.telemetry
    data, setups = latest_derivatives(session)
    assert data["oi_history_source"] == "SELF_RECORDED"
    assert data["interpretation"] == "unavailable" and data["oi_change_pct"] is None
    assert set(data["oi_change_by_horizon"].values()) == {None}
    assert all(s["blocks"]["derivatives"] <= 4 for s in setups)  # no constructive OI

    # Same boundary again: nothing duplicated.
    again = await scan(session, SelfRecordingExchange(Decimal(1000)), first)
    assert "oi_snapshots_kept" not in again.telemetry
    assert session.scalar(select(func.count()).select_from(OpenInterestSnapshot)) == 2

    # Restart (new session and runtime) one boundary later: stored history is reused.
    collector = OICollectorService(
        ScannerRuntime(SelfRecordingExchange(Decimal(1100)), Settings()),
        get_session_factory(sqlite_database_url),
        clock=lambda: first + STEP,
    )
    await collector.collect(B + STEP)
    with get_session_factory(sqlite_database_url)() as restarted:
        await scan(restarted, SelfRecordingExchange(Decimal(1100)), first + STEP)
        data, _ = latest_derivatives(restarted)
    assert data["oi_change_pct"] == pytest.approx(10.0)
    assert data["oi_change_by_horizon"]["15m"] == pytest.approx(10.0)
    assert data["oi_change_by_horizon"]["1h"] is None
    assert data["interpretation"] != "unavailable"


async def test_unaligned_observation_is_not_stored(session):
    run = await scan(
        session, SelfRecordingExchange(offset=400), B + timedelta(seconds=400)
    )
    assert "oi_observations_unaligned" not in run.telemetry
    assert session.scalar(select(func.count()).select_from(OpenInterestSnapshot)) == 0


async def test_exchange_with_published_history_records_nothing(session):
    await scan(session, FixtureFutures(), B + timedelta(seconds=60))
    assert session.scalar(select(func.count()).select_from(OpenInterestSnapshot)) == 0
