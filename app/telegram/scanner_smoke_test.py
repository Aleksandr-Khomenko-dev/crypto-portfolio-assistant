"""Scanner-to-Telegram integration check with an injected READY result.

Usage: python -m app.telegram.scanner_smoke_test

The injected result travels the production path: ScannerRepository.save (episode and
dedup state) -> deliver_notifications (should_notify, format_setup, cooldown, failure
handling) -> TelegramService.send_scanner -> Telegram Bot API. Every message is
labelled as a test. State lives in a throwaway SQLite file, never the configured
database. No market data is fetched, nothing is scored and nothing is traded.

Sequence (production notification rules, unchanged):
  1. READY setup, score 85        -> first alert: expected SENT
  2. identical event again        -> expected SUPPRESSED (duplicate / cooldown)
  3. score 92 (EXTREME), +5 min   -> state upgrade inside cooldown: expected SUPPRESSED
  4. closed candle through the fixed invalidation, +16 min -> expected SENT (INVALIDATED)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import models  # noqa: F401 - register all tables
from app.db.base import Base
from app.db.scanner_models import MarketSetup, ScannerRun
from app.scanner.domain import (
    Candle,
    Derivatives,
    Direction,
    FrameAnalysis,
    MarketContext,
    Readiness,
    RiskPlan,
    ScannerResult,
    Setup,
    Structure,
    StructureBreak,
    Technical,
    Zone,
)
from app.scanner.notifications import deliver_notifications, should_notify
from app.scanner.repository import ScannerRepository
from app.scanner.scoring import signal_state
from app.telegram.smoke_test import missing_settings, safe_error

SYMBOL = "ARBUSDT"
ENTRY, INVALIDATION, RESISTANCE = (
    Decimal("0.5000"),
    Decimal("0.4800"),
    Decimal("0.5400"),
)


def _candle(close_time: datetime, close: Decimal, minutes: int = 15) -> Candle:
    return Candle(
        open_time=close_time + timedelta(milliseconds=1) - timedelta(minutes=minutes),
        close_time=close_time,
        open=close,
        high=close + Decimal("0.002"),
        low=close - Decimal("0.002"),
        close=close,
        volume=Decimal(1000),
    )


def _frame(timeframe: str, close_time: datetime) -> FrameAnalysis:
    def zone(lower: str, upper: str, distance_atr: float) -> Zone:
        return Zone(
            lower=Decimal(lower),
            upper=Decimal(upper),
            created_at=close_time,
            age_bars=10,
            interactions=2,
            distance_atr=distance_atr,
        )

    return FrameAnalysis(
        timeframe=timeframe,  # type: ignore[arg-type]
        candle=_candle(close_time, ENTRY),
        technical=Technical(
            ema={"8": 0.5, "21": 0.49, "50": 0.48, "200": 0.45},
            price_above_ema={"8": True, "21": True, "50": True, "200": True},
            ema_slope_atr={"8": 0.2, "21": 0.1, "50": 0.05, "200": 0.01},
            alignment="BULLISH",
            expansion="EXPANDING",
            atr=0.008,
            atr_pct=1.6,
            rvol=1.8,
            rsi=58.0,
            adx=28.0,
            plus_di=27.0,
            minus_di=12.0,
            extension_atr=0.6,
            momentum_pct=1.2,
        ),
        macro=Structure(trend="BULLISH"),
        micro=Structure(
            trend="BULLISH",
            breaks=[
                StructureBreak(
                    index=0,
                    timestamp=close_time - timedelta(minutes=15),
                    level=Decimal("0.4950"),
                    direction=Direction.LONG,
                    kind="BOS",
                )
            ],
        ),
        supports=[zone("0.4850", "0.4900", 1.2)],
        resistances=[zone(str(RESISTANCE), "0.5450", 5.0)],
        fvgs=[],
    )


def synthetic_ready_result(
    created_at: datetime, candle_closed_at: datetime, score: int, settings: Settings
) -> ScannerResult:
    """A fixed, clearly synthetic READY LONG result; no scoring code is executed."""
    blocks = {
        "regime": 15,
        "structure": 20,
        "location": 12,
        "trigger": 15,
        "volume_momentum": 10,
        "derivatives": 6,
        "market_context": 5,
        "risk_room": 10,
    }
    blocks["location"] -= sum(blocks.values()) - score  # make the blocks sum to score
    setup = Setup(
        symbol=SYMBOL,
        direction=Direction.LONG,
        score=score,
        state=signal_state(score, settings),
        readiness=Readiness.READY,
        next_condition="Integration test fixture; not a market observation",
        blocks=blocks,
        risk=RiskPlan(
            invalidation=INVALIDATION,
            stop_distance=ENTRY - INVALIDATION,
            stop_atr=2.5,
            nearest_obstacle=RESISTANCE,
            rr=2.0,
            valid=True,
            reason="VALID",
        ),
    )
    frames = {tf: _frame(tf, candle_closed_at) for tf in ("15m", "1h", "4h")}
    return ScannerResult(
        symbol=SYMBOL,
        price=ENTRY,
        observed_price=ENTRY,
        observed_at=created_at,
        candle_closed_at=candle_closed_at,
        created_at=created_at,
        expires_at=created_at
        + timedelta(minutes=settings.scanner_setup_expiry_minutes),
        long_score=score,
        short_score=0,
        setups=[setup],
        frames=frames,  # type: ignore[arg-type]
        derivatives=Derivatives(funding_state="NEUTRAL"),
        context=MarketContext(state="BULLISH", explanation="Integration test fixture"),
        exchange=settings.scanner_provider.upper(),
    )


async def run_sequence(
    settings: Settings,
    session: Session,
    report: Callable[[str], None] = print,
    now: datetime | None = None,
) -> list[bool]:
    """Returns, per step, whether a Telegram message was recorded as delivered."""
    now = now or datetime.now(UTC)
    closed = datetime.fromtimestamp(now.timestamp() // 900 * 900, UTC) - timedelta(
        milliseconds=1
    )
    repository = ScannerRepository(session)
    run = ScannerRun(started_at=now, config_json={"integration_test": True}, errors={})
    session.add(run)
    session.commit()
    ready = synthetic_ready_result(now, closed, 85, settings)
    upgraded = synthetic_ready_result(now + timedelta(minutes=5), closed, 92, settings)
    breaking = _candle(now + timedelta(minutes=15), INVALIDATION - Decimal("0.001"))
    invalidated = synthetic_ready_result(
        now + timedelta(minutes=16), breaking.close_time, 92, settings
    )
    steps = [
        ("1 READY score 85 (first event)", ready, [], now),
        ("2 identical READY event again", ready, [], now),
        ("3 upgrade to score 92 EXTREME (+5 min)", upgraded, [], upgraded.created_at),
        (
            "4 closed candle through fixed invalidation (+16 min)",
            invalidated,
            [breaking],
            invalidated.created_at,
        ),
    ]
    delivered = []
    for name, result, bars, at in steps:
        repository.save(run.id, result, settings, bars)
        session.commit()
        setup = session.query(MarketSetup).one()
        due = should_notify(setup, at, settings)
        sent = await deliver_notifications(session, settings, at, mode="TEST") > 0
        session.refresh(setup)
        outcome = (
            "SENT"
            if sent
            else f"FAILED ({setup.delivery_error})"
            if due
            else "SUPPRESSED"
        )
        report(
            f"Step {name}: lifecycle={setup.lifecycle} state={setup.state} "
            f"readiness={setup.readiness} due={due} -> {outcome}"
        )
        delivered.append(sent)
    return delivered


async def main_async() -> int:
    settings = get_settings()
    missing = missing_settings(settings)
    if missing:
        print(
            "Scanner Telegram path NOT exercised. Missing settings: "
            + ", ".join(missing)
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="scanner-smoke-") as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'integration.db'}")
        Base.metadata.create_all(engine)
        try:
            with Session(engine, expire_on_commit=False) as session:
                delivered = await run_sequence(settings, session)
        except Exception as exc:  # noqa: BLE001 - report safely, never the token
            print("Scanner Telegram path FAILED: " + safe_error(exc))
            return 1
        finally:
            engine.dispose()
    expected = [True, False, False, True]
    print(
        "Scanner Telegram path "
        + (
            "matched expected SENT/SUPPRESSED/SUPPRESSED/SENT"
            if delivered == expected
            else f"differs from expectation: {delivered}"
        )
    )
    return 0 if delivered == expected else 1


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
