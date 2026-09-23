from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import sin

from app.scanner.domain import Candle, Contract, Derivatives, INTERVAL_SECONDS, Ticker


def candles(timeframe="15m", now=None, count=240, slope=0.05):
    now = now or datetime(2026, 9, 23, 12, 0, 10, tzinfo=timezone.utc)
    step = INTERVAL_SECONDS[timeframe]
    end = datetime.fromtimestamp(int(now.timestamp()) // step * step, timezone.utc)
    result = []
    for i in range(count):
        price = Decimal(str(100 + slope * i + 2 * sin(i / 3)))
        start = end - timedelta(seconds=(count - i) * step)
        result.append(
            Candle(
                open_time=start,
                close_time=start + timedelta(seconds=step, milliseconds=-1),
                open=price - Decimal("0.1"),
                high=price + 1,
                low=price - 1,
                close=price,
                volume=100 + i % 5,
            )
        )
    return result


def price_bars(prices):
    bars = candles(count=len(prices))
    return [
        b.model_copy(
            update={
                "open": Decimal(str(p)),
                "close": Decimal(str(p)),
                "high": Decimal(str(p)) + Decimal(".2"),
                "low": Decimal(str(p)) - Decimal(".2"),
            }
        )
        for b, p in zip(bars, prices)
    ]


class FixtureFutures:
    def __init__(self, fail=(), derivatives_fail=False):
        self.fail = fail
        self.derivatives_fail = derivatives_fail
        self.closed = False

    async def contracts(self):
        return [
            Contract(
                symbol=s,
                base_asset=s.removesuffix("USDT"),
                quote_asset="USDT",
                contract_type="PERPETUAL",
                status="TRADING",
            )
            for s in ("AAAUSDT", "BBBUSDT")
        ]

    async def tickers(self):
        return {
            s: Ticker(
                symbol=s,
                price=112,
                quote_volume=20_000_000,
                timestamp=datetime.now(timezone.utc),
            )
            for s in ("AAAUSDT", "BBBUSDT")
        }

    async def candles(self, symbol, timeframe, now):
        if symbol in self.fail:
            raise RuntimeError("fixture failure")
        return candles(timeframe, now)

    async def derivatives(self, symbol, now):
        if self.derivatives_fail:
            raise RuntimeError("derivatives unavailable")
        return Derivatives(
            funding_rate=Decimal(".0001"),
            open_interest=1000,
            oi_timestamp=now,
            funding_timestamp=now,
        )

    async def aclose(self):
        self.closed = True
