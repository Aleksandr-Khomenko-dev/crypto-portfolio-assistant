"""Download free Dukascopy XAU/USD minute history for the backtest (research only).

    python -m scripts.dukascopy_gold --start 2019-01-01

Fetches Dukascopy's public daily BID 1-minute candle files (no account needed),
caches them in data/backtest/dukascopy/, and writes 5m/15m/1h/4h/1d candles to
data/backtest/XAUUSD_<tf>.json in the format scripts.backtest_gold reads, so the
replay runs with `python -m scripts.backtest_gold --symbol XAUUSD`.

Dukascopy fills closed hours and weekends with flat zero-volume minutes, so the
series is continuous like BingX's 24/7 gold perpetual. Volume is Dukascopy's own
(relative volume only is used by the scanner). Prices are spot XAU/USD BID, not
the BingX perpetual, so levels differ by a small basis.
"""

from __future__ import annotations

import argparse
import json
import lzma
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from app.scanner.domain import INTERVAL_SECONDS, Candle

URL = "https://datafeed.dukascopy.com/datafeed/{inst}/{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
CACHE = Path("data/backtest")
RAW = CACHE / "dukascopy"
SCALE = 3  # XAU/USD prices are integers in thousandths
Minute = tuple[int, int, int, int, int, float]  # epoch s, open, high, low, close, vol


def fetch_day(client: httpx.Client, inst: str, day: date) -> bytes | None:
    path = RAW / inst / f"{day.isoformat()}.bi5"
    if path.exists():
        return path.read_bytes()
    url = URL.format(inst=inst, y=day.year, m=day.month - 1, d=day.day)
    for attempt in range(4):
        try:
            response = client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            path.write_bytes(response.content)
            return response.content
        except httpx.HTTPError:
            time.sleep(2 * (attempt + 1))
    return None


def parse_day(day: date, blob: bytes | None) -> list[Minute]:
    if not blob:
        return []
    raw = lzma.decompress(blob)
    base = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    out = []
    for offset in range(0, len(raw), 24):
        sec, o, c, lo, hi, vol = struct.unpack(">5if", raw[offset : offset + 24])
        out.append((base + sec, o, hi, lo, c, vol))
    return out


def aggregate(minutes: list[Minute], timeframe: str) -> list[Candle]:
    step = INTERVAL_SECONDS[timeframe]
    buckets: dict[int, list[Minute]] = {}
    for m in minutes:
        buckets.setdefault(m[0] - m[0] % step, []).append(m)
    bars = []
    for start, rows in sorted(buckets.items()):
        if len(rows) != step // 60:
            continue  # incomplete bucket (missing file) -> leave a visible gap
        opened = datetime.fromtimestamp(start, UTC)
        bars.append(
            Candle(
                open_time=opened,
                close_time=opened + timedelta(seconds=step, milliseconds=-1),
                open=Decimal(rows[0][1]).scaleb(-SCALE),
                high=Decimal(max(r[2] for r in rows)).scaleb(-SCALE),
                low=Decimal(min(r[3] for r in rows)).scaleb(-SCALE),
                close=Decimal(rows[-1][4]).scaleb(-SCALE),
                volume=Decimal(str(round(sum(r[5] for r in rows), 6))),
            )
        )
    return bars


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--instrument", default="XAUUSD")
    args = parser.parse_args()
    inst = args.instrument.upper()
    (RAW / inst).mkdir(parents=True, exist_ok=True)
    first, last = date.fromisoformat(args.start), datetime.now(UTC).date()
    days = [first + timedelta(days=n) for n in range((last - first).days)]
    started = time.monotonic()
    with httpx.Client(timeout=30) as client, ThreadPoolExecutor(6) as pool:
        blobs = list(pool.map(lambda d: fetch_day(client, inst, d), days))
    missing = [d.isoformat() for d, b in zip(days, blobs, strict=True) if b is None]
    minutes = [m for d, b in zip(days, blobs, strict=True) for m in parse_day(d, b)]
    minutes.sort()
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        bars = aggregate(minutes, tf)
        (CACHE / f"{inst}_{tf}.json").write_text(
            json.dumps([b.model_dump(mode="json") for b in bars])
        )
        print(f"{tf}: {len(bars)} bars {bars[0].open_time} .. {bars[-1].open_time}")
    print(
        f"days={len(days)} missing={len(missing)} {missing[:10]} "
        f"elapsed={time.monotonic() - started:.0f}s"
    )


if __name__ == "__main__":
    main()
