"""Walk-forward replay of the scanner on BingX history (research only, no trading).

    python -m scripts.backtest_gold                 # gold, all cached/available history
    python -m scripts.backtest_gold --refresh       # re-download candles first

For every CLOSED 15m candle while the market is open it runs the bot's own
analyze_frame (15m/1h/4h) and score_setup, exactly as the live scanner does, and
opens a paper trade at the first READY of a direction: entry = that candle's close,
stop = the setup's invalidation, targets 0.5R/1R/1.5R/2R, tracked by the live
outcome engine (app/research/outcomes.py) for 96 bars, same-bar ambiguity resolved
with 5m candles when BingX still has them.

Not replayed (no history): funding/OI (derivatives block = 0, so scores are up to
10 points lower than live), the DXY/US10Y/VIX macro veto and the calendar pause.

Candles are cached in data/backtest/ (git-ignored); trades go to
data/backtest/<symbol>_trades.json.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.analytics.analysis import analyze_frame
from app.config import get_settings
from app.providers.bingx_futures import internal_to_bingx_symbol, parse_candle
from app.research.outcomes import TRACKING, Progress, advance, build_plan
from app.scanner.domain import (
    INTERVAL_SECONDS,
    Candle,
    Derivatives,
    Direction,
    MarketContext,
    Readiness,
    Ticker,
)
from app.scanner.market_hours import NEW_YORK, metals_market_open
from app.scanner.scoring import build_result

URL = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
CACHE = Path("data/backtest")
WINDOW = 300  # bars per frame, as scanner_candle_limit
HORIZON = 96  # 15m bars (24 h), as outcome_max_bars_15m


def download(symbol: str, timeframe: str) -> list[Candle]:
    """Every candle BingX still serves, paging back from now (1000 per request)."""
    rows: dict[int, dict[str, Any]] = {}
    end = int(time.time() * 1000)
    with httpx.Client(timeout=30) as client:
        while True:
            params = {
                "symbol": internal_to_bingx_symbol(symbol),
                "interval": timeframe,
                "endTime": end,
                "limit": 1000,
            }
            data = client.get(URL, params=params).json().get("data") or []
            fresh = [r for r in data if int(r["time"]) not in rows]
            if not fresh:
                break
            for r in fresh:
                rows[int(r["time"])] = r
            end = min(int(r["time"]) for r in fresh) - 1
            time.sleep(0.25)
    bars = [parse_candle(r, timeframe) for r in rows.values()]  # type: ignore[arg-type]
    return sorted(bars, key=lambda b: b.open_time)


def candles(symbol: str, timeframe: str, refresh: bool) -> list[Candle]:
    path = CACHE / f"{symbol}_{timeframe}.json"
    if path.exists() and not refresh:
        return [Candle.model_validate(b) for b in json.loads(path.read_text())]
    CACHE.mkdir(parents=True, exist_ok=True)
    bars = download(symbol, timeframe)
    path.write_text(json.dumps([b.model_dump(mode="json") for b in bars]))
    return bars


def closed_until(bars: list[Candle], now: datetime, start: int) -> int:
    """Index one past the last bar closed by `now`, scanning forward from `start`."""
    while start < len(bars) and bars[start].close_time < now:
        start += 1
    return start


def replay(symbol: str, refresh: bool) -> list[dict[str, Any]]:
    settings = get_settings()
    data = {tf: candles(symbol, tf, refresh) for tf in ("5m", "15m", "1h", "4h")}
    children: dict[datetime, list[Candle]] = {}
    for bar in data["5m"]:
        parent = bar.open_time - timedelta(
            seconds=bar.open_time.timestamp() % INTERVAL_SECONDS["15m"]
        )
        children.setdefault(parent, []).append(bar)
    m15, frames_cache = data["15m"], {}
    cursor = {"1h": 0, "4h": 0}
    previous: dict[str, Any] = {}
    open_trades: dict[Direction, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    grace = timedelta(seconds=settings.scanner_data_grace_seconds + 1)
    for i in range(WINDOW, len(m15)):
        bar = m15[i]
        now = bar.open_time + timedelta(minutes=15) + grace
        # Open trades advance on every closed bar, market open or not (as live).
        for direction, trade in list(open_trades.items()):
            step = advance(
                trade["plan"],
                trade["progress"],
                [bar],
                now,
                HORIZON,
                {bar.open_time: children.get(bar.open_time, [])},
            )
            trade["progress"] = step.progress
            if step.progress.outcome_status != TRACKING:
                trades.append(finish(trade))
                del open_trades[direction]
        if not metals_market_open(now):
            continue
        frames = {}
        try:
            frames["15m"] = analyze_frame(
                m15[i - WINDOW + 1 : i + 1], "15m", now, settings, previous.get("15m")
            )
            for tf in ("1h", "4h"):
                end = cursor[tf] = closed_until(data[tf], now, cursor[tf])
                if end < WINDOW:
                    raise ValueError("warming up")
                key = (tf, end)
                if key not in frames_cache:
                    frames_cache.clear() if len(frames_cache) > 8 else None
                    frames_cache[key] = analyze_frame(
                        data[tf][end - WINDOW : end],
                        tf,
                        now,
                        settings,
                        previous.get(tf),
                    )
                frames[tf] = frames_cache[key]
        except ValueError:
            continue
        previous = frames
        ticker = Ticker(symbol=symbol, price=bar.close, quote_volume=0, timestamp=now)
        context = MarketContext(kind="GOLD_MACRO", explanation="no macro history")
        result = build_result(
            symbol, frames, Derivatives(), context, ticker, now, settings
        )
        for setup in result.setups:
            if (
                setup.readiness != Readiness.READY
                or setup.risk.invalidation is None
                or setup.direction in open_trades
            ):
                continue
            plan = build_plan(
                setup.direction, "15m", bar.close, setup.risk.invalidation
            )
            if plan is None:
                continue
            open_trades[setup.direction] = {
                "plan": plan,
                "progress": Progress(
                    last_processed_at=bar.close_time,
                    max_favorable_price=plan.entry,
                    max_adverse_price=plan.entry,
                ),
                "features": features(setup, result, bar, now),
            }
    return trades


def features(setup, result, bar: Candle, now: datetime) -> dict[str, Any]:
    f15, f1h, f4h = (result.frames[tf] for tf in ("15m", "1h", "4h"))
    trend = "BULLISH" if setup.direction == Direction.LONG else "BEARISH"
    return {
        "at": bar.close_time.isoformat(),
        "hour_ny": now.astimezone(NEW_YORK).hour,
        "weekday": now.astimezone(NEW_YORK).weekday(),
        "direction": setup.direction.value,
        "score": setup.score,
        "blocks": dict(setup.blocks),
        "entry": str(bar.close),
        "stop": str(setup.risk.invalidation),
        "stop_atr": setup.risk.stop_atr,
        "rr": setup.risk.rr,
        "trend_4h_with": f4h.macro.trend == trend,
        "trend_1h_with": f1h.macro.trend == trend,
        "micro_15m_with": f15.micro.trend == trend,
        "rsi": f15.technical.rsi,
        "adx": f15.technical.adx,
        "rvol": f15.technical.rvol,
        "atr_pct": f15.technical.atr_pct,
        "ema_15m": f15.technical.alignment,
        "ema_1h": f1h.technical.alignment,
        "ema_4h": f4h.technical.alignment,
    }


def finish(trade: dict[str, Any]) -> dict[str, Any]:
    p: Progress = trade["progress"]
    return {
        **trade["features"],
        "status": p.outcome_status,
        "first": dict(p.first),
        "bars_to": dict(p.bars_to),
        "mfe_r": p.max_favorable_excursion_r,
        "mae_r": p.max_adverse_excursion_r,
        "ambiguity": p.ambiguity_resolution,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NCCOGOLD2USDUSDT")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    trades = replay(args.symbol.upper(), args.refresh)
    out = CACHE / f"{args.symbol.upper()}_trades.json"
    out.write_text(json.dumps(trades, indent=1, default=str))
    wins = sum(t["first"]["1r"] == "TARGET_FIRST" for t in trades)
    losses = sum(t["first"]["1r"] == "INVALIDATION_FIRST" for t in trades)
    print(
        f"trades={len(trades)} 1R first={wins} stop first={losses} "
        f"other={len(trades) - wins - losses} "
        f"elapsed={time.monotonic() - started:.0f}s -> {out}"
    )


if __name__ == "__main__":
    main()
