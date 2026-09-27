"""SYNTHETIC early-event sequence -> Telegram, clearly labelled TEST.

Usage: python -m scripts.early_synthetic_test DB_PATH [--send]

Replays a fabricated GAS-like scenario (1H support -> ignition -> breakout approach ->
first break -> retest -> retest confirmed) through the real FastMarketWatcher code
path in TEST mode. It uses its OWN throwaway SQLite database (DB_PATH, must not
exist) and no market data at all, so nothing touches the production database or is
mistaken for a live signal. Every message carries "TEST — NOT A REAL TRADING SIGNAL".
Without --send, messages are rendered but not delivered. The token and chat id are
never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine

from app.config import get_settings
from app.db import models  # noqa: F401 - register tables
from app.db.base import Base
from app.db.session import get_session_factory
from app.early.context import StructureContext
from app.early.model import KeyZone
from app.fast.detector import FastConfig, SymbolTracker
from app.fast.watcher import EarlyJob, FastMarketWatcher, chat_fingerprint
from app.scanner.domain import Candle, Pivot, StructureBreak

SYMBOL = "GASUSDT"  # synthetic scenario name only; no GAS market data is used


def bars(closes: list[float], end: float, sweep: int | None = None) -> list[Candle]:
    result, previous = [], closes[0]
    for k, close in enumerate(closes):
        start = datetime.fromtimestamp(end - (len(closes) - k) * 300, UTC)
        low = min(previous, close) - 0.15
        result.append(
            Candle(
                open_time=start,
                close_time=start + timedelta(seconds=300, milliseconds=-1),
                open=Decimal(str(previous)),
                high=Decimal(str(max(previous, close) + 0.15)),
                low=Decimal(str(98.8 if k == sweep else low)),
                close=Decimal(str(close)),
                volume=Decimal(100),
            )
        )
        previous = close
    return result


class Offline:
    """No exchange access: OI is 'нет данных' in every synthetic message."""

    async def open_interest_notional(self, symbol: str) -> Decimal:
        raise RuntimeError("synthetic test: no market data")


async def run(
    db_path: Path, send: bool, show: bool = False
) -> list[tuple[str, int | None]]:
    settings = get_settings()
    url = f"sqlite:///{db_path}"
    Base.metadata.create_all(create_engine(url))
    chat = settings.scanner_telegram_chat_id
    rendered: list[str] = []

    async def sender(text, reply_to, buttons):  # dry run: render only, no delivery
        rendered.append(text)
        return []

    if send:
        from app.services.telegram_service import TelegramService

        telegram = TelegramService(settings)
        if not (telegram.enabled and chat):
            raise SystemExit("Telegram is not configured (token/chat id)")

        async def sender(text, reply_to, buttons):  # type: ignore[no-redef]
            rendered.append(text)
            return await telegram.send_message(chat, text, None, buttons)

    watcher = FastMarketWatcher(
        settings,
        get_session_factory(url),
        Offline(),
        sender,
        mode="TEST",
        destination=chat_fingerprint(chat),
    )
    never = dict.fromkeys(("15s", "30s", "1m", "3m", "5m"), 99.0)
    watcher.config = FastConfig(min_pct=never, extreme_pct=never)  # structure only
    assert watcher.early is not None
    t0 = time.time() // 300 * 300
    atr = 2.5
    support = KeyZone("1h", "SUPPORT", 99.0, 99.5, None, 3, atr)
    resistance = KeyZone("1h", "RESISTANCE", 104.0, 104.5, None, 3, atr)
    decline = [101.5, 101.0, 100.6, 100.2, 99.9, 99.6, 99.3, 99.1, 99.3]
    reaction = [*decline, 99.6, 99.8, 99.9]

    def context(closes, shift=False) -> StructureContext:
        five = bars(closes, t0, sweep=7)
        extra = {}
        if shift:
            extra = {
                "breaks5": [
                    StructureBreak(
                        index=0,
                        timestamp=five[-2].close_time,
                        level=five[-3].high,
                        direction="LONG",
                        kind="CHoCH",
                    )
                ],
                "pivots5": [
                    Pivot(
                        index=0,
                        confirmed_index=0,
                        price=five[-4].low,
                        kind="LOW",
                        label="HL",
                        timestamp=five[-4].close_time,
                        confirmed_at=five[-2].close_time,
                    )
                ],
                "rvol5": 2.0,
            }
        return StructureContext(
            symbol=SYMBOL,
            built_at=five[-1].close_time,
            atr={"5m": 0.8, "15m": 1.4, "1h": atr, "4h": 5.0},
            zones=[support, resistance],
            bars5=five,
            oi_change_15m=None,
            market={"BTC": "neutral", "ETH": "neutral"},
            **extra,
        )

    def baseline(level: float) -> SymbolTracker:
        """Calm 1m baseline (0.05% realised volatility) at the scene's price."""
        tracker = SymbolTracker(SYMBOL)
        first = int(t0 // 60 * 60 - 3600) * 1000
        tracker.seed(
            [
                (first + i * 60_000, level * (1 + 0.0005 * (-1) ** i), 1000.0)
                for i in range(60)
            ]
        )
        return tracker

    watcher.trackers[SYMBOL] = baseline(99.3)
    sent: list[tuple[str, int | None]] = []
    at = t0 + 10
    state = {"minute": None, "total": 0.0}

    async def drain() -> None:
        while watcher.outbox.qsize():
            _, job = await watcher.outbox.get()
            started = (
                time.perf_counter()
            )  # real wall clock (the replay clock is simulated)
            await watcher.dispatch(job)
            if isinstance(job, EarlyJob):
                sent.append(
                    (
                        job.event.event_type.value,
                        round((time.perf_counter() - started) * 1000),
                    )
                )

    def tick(price: float, count: int) -> None:
        nonlocal at
        for _ in range(count):
            minute = int(at // 60 * 60) * 1000
            if minute != state["minute"]:
                state["minute"], state["total"] = minute, 0.0
            state["total"] += 30  # busy tape: volume ~1.8x normal
            watcher.clock = lambda now=at: now
            watcher.on_kline(
                "GAS-USDT", minute, price, state["total"], at, price, price, price
            )
            at += 1

    # 1. price arrives into the 1H support (ZONE_WATCH)
    watcher.early.set_context(context(decline), 101.5, at)
    tick(101.5, 65)  # seen live well above the support ...
    tick(100.4, 2)
    tick(99.3, 5)  # ... then arriving into it
    await drain()
    # 2. reaction: sweep + reclaim + 5m CHoCH + higher low (BULLISH_IGNITION)
    for event in watcher.early.set_context(context(reaction, shift=True), 99.9, at):
        watcher._enqueue_early(event, None)
    await drain()
    # 3-6. approach, first break, retest, retest confirmed at the 1H resistance
    # New scene: the two scenes are spliced, so the breakout scene gets a baseline
    # at its own price (otherwise the splice itself would look like volatility).
    watcher.trackers[SYMBOL] = baseline(102.0)
    for price, count in (
        (102.0, 70),
        (102.6, 2),
        (103.2, 2),
        (103.8, 3),
        (104.3, 5),
        (104.8, 15),
        (105.3, 10),
        (105.0, 3),
        (104.9, 5),
        (105.45, 5),
        (105.5, 20),
    ):
        tick(price, count)
        await drain()
    if show:
        print("\n\n".join(rendered))
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--show", action="store_true", help="print rendered messages")
    args = parser.parse_args()
    if args.db_path.exists():
        sys.exit("Refusing to reuse an existing database file (synthetic data only).")
    sent = asyncio.run(run(args.db_path, args.send, args.show))
    print("SYNTHETIC TEST sequence:", " -> ".join(kind for kind, _ in sent))
    print("dispatch wall-clock ms:", [ms for _, ms in sent])
    from sqlalchemy import select

    from app.db.scanner_models import FastMarketEvent

    with get_session_factory(f"sqlite:///{args.db_path}")() as session:
        for row in session.scalars(
            select(FastMarketEvent).order_by(FastMarketEvent.detected_at)
        ):
            print(
                f"  {row.event_type:<18} sent={row.sent} message_id={row.telegram_message_id}"
                f" notify={(row.context or {}).get('notify', {}).get('reason')}"
                f" error={row.error}"
            )


if __name__ == "__main__":
    main()
