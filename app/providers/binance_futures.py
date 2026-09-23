from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.providers.request_control import RequestBudget
from app.scanner.domain import (
    INTERVAL_SECONDS,
    Candle,
    Contract,
    Derivatives,
    OIPoint,
    Ticker,
    Timeframe,
)

logger = logging.getLogger(__name__)


def timestamp(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def request_weight(path: str, params: dict[str, Any] | None) -> int:
    """Binance USD-M request weights for the endpoints used here."""
    if path == "/fapi/v1/ticker/24hr":
        return 1 if params and "symbol" in params else 40
    if path == "/fapi/v1/premiumIndex":
        return 1 if params and "symbol" in params else 10
    if path == "/fapi/v1/klines":
        limit = int((params or {}).get("limit", 500))
        return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10
    return 1


class BinanceFuturesProvider:
    """Reusable public GET client, paced globally with incremental closed-bar caching."""

    exchange = "BINANCE"

    def __init__(
        self, settings: Settings, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            base_url=settings.binance_futures_base_url,
            timeout=settings.http_timeout_seconds,
            limits=httpx.Limits(max_connections=settings.scanner_concurrency),
        )
        self._semaphore = asyncio.Semaphore(settings.scanner_concurrency)
        self._budget = RequestBudget(
            settings.scanner_requests_per_second, settings.scanner_weight_per_minute
        )
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._bar_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._bars: dict[tuple[str, str], list[Candle]] = {}
        # Cumulative operational counters; the scanner records per-run deltas.
        self.stats: Counter[str] = Counter()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(self.settings.scanner_http_retries + 1):
            async with self._semaphore:
                weight = request_weight(path, params)
                await self._budget.acquire(weight)
                try:
                    self.stats["requests"] += 1
                    response = await self.client.get(path, params=params)
                    if response.status_code in (418, 429):
                        self.stats[f"http_{response.status_code}"] += 1
                        # Share the exchange's cooldown across all symbols, including queued requests.
                        try:
                            delay = max(
                                1, float(response.headers.get("Retry-After", "60"))
                            )
                        except ValueError:
                            delay = 60
                        self._budget.defer(delay)
                    used = response.headers.get("X-MBX-USED-WEIGHT-1M", "0")
                    if (
                        used.isdigit()
                        and int(used) >= self.settings.scanner_weight_per_minute
                    ):
                        self._budget.defer(60)
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (418, 429, 500, 502, 503, 504):
                        raise
                    if attempt == self.settings.scanner_http_retries:
                        raise
                except httpx.TransportError:
                    if attempt == self.settings.scanner_http_retries:
                        raise
            logger.warning(
                "Futures transient request failure: %s attempt=%s", path, attempt + 1
            )
            await asyncio.sleep(2**attempt)
        raise RuntimeError("Unreachable retry state")

    async def _cached(
        self, path: str, ttl: int, params: dict[str, Any] | None = None
    ) -> Any:
        key = path + repr(sorted((params or {}).items()))
        async with self._cache_locks.setdefault(key, asyncio.Lock()):
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                self.stats["cache_hits"] += 1
                return cached[1]
            self.stats["cache_misses"] += 1
            data = await self._get(path, params)
            now = time.monotonic()
            # Time-bucketed keys (e.g. openInterestHist endTime) must not accumulate forever.
            for stale in [k for k, (expiry, _) in self._cache.items() if expiry <= now]:
                if not self._cache_locks[stale].locked():
                    del self._cache[stale], self._cache_locks[stale]
            self._cache[key] = (now + ttl, data)
            return data

    async def contracts(self) -> list[Contract]:
        data = await self._cached("/fapi/v1/exchangeInfo", 3600)
        result = []
        for row in data["symbols"]:
            try:
                result.append(
                    Contract(
                        symbol=row["symbol"],
                        base_asset=row["baseAsset"],
                        quote_asset=row["quoteAsset"],
                        contract_type=row["contractType"],
                        status=row["status"],
                        underlying_type=row.get("underlyingType", "COIN"),
                        underlying_subtypes=row.get("underlyingSubType", []),
                    )
                )
            except (KeyError, ValidationError):
                logger.warning("Ignoring malformed futures contract metadata")
        return result

    async def tickers(self) -> dict[str, Ticker]:
        data = await self._cached(
            "/fapi/v1/ticker/24hr", self.settings.scanner_universe_cache_seconds
        )
        result = {}
        for row in data:
            try:
                ticker = Ticker(
                    symbol=row["symbol"],
                    price=row["lastPrice"],
                    quote_volume=row["quoteVolume"],
                    timestamp=timestamp(row["closeTime"]),
                )
                result[ticker.symbol] = ticker
            except (KeyError, ValueError):
                logger.warning("Ignoring malformed futures ticker")
        return result

    async def candles(
        self, symbol: str, timeframe: Timeframe, now: datetime
    ) -> list[Candle]:
        async with self._bar_locks.setdefault((symbol, timeframe), asyncio.Lock()):
            return await self._candles(symbol, timeframe, now)

    async def _candles(
        self, symbol: str, timeframe: Timeframe, now: datetime
    ) -> list[Candle]:
        interval = INTERVAL_SECONDS[timeframe]
        server = await self._cached("/fapi/v1/time", 30)
        now = min(now, timestamp(server["serverTime"]))
        # Request an explicit boundary to prevent an in-flight candle entering confirmed logic.
        end_ms = (
            int(
                (now.timestamp() - self.settings.scanner_data_grace_seconds)
                // interval
                * interval
                * 1000
            )
            - 1
        )
        key = (symbol, timeframe)
        existing = self._bars.get(key, [])
        if existing and int(existing[-1].close_time.timestamp() * 1000) >= end_ms:
            self.stats["candle_cache_hits"] += 1
            return [bar for bar in existing if bar.close_time <= timestamp(end_ms)]
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": timeframe,
            "limit": self.settings.scanner_candle_limit,
            "endTime": end_ms,
        }
        if existing and (
            end_ms / 1000 - existing[-1].close_time.timestamp()
        ) < interval * (self.settings.scanner_candle_limit - 1):
            params["startTime"] = int(existing[-1].open_time.timestamp() * 1000)
        else:
            existing = []
        self.stats["candle_cache_misses"] += 1
        payload = await self._get("/fapi/v1/klines", params)
        fresh = [
            Candle(
                open_time=timestamp(r[0]),
                close_time=timestamp(r[6]),
                open=r[1],
                high=r[2],
                low=r[3],
                close=r[4],
                volume=r[5],
            )
            for r in payload
            if r[6] <= end_ms
        ]
        merged = {bar.open_time: bar for bar in existing + fresh}
        result = sorted(merged.values(), key=lambda bar: bar.open_time)[
            -self.settings.scanner_candle_limit :
        ]
        # Previously closed observations are immutable. A revision fails this cycle and
        # drops the cache so the next cycle rebuilds from a clean full history instead
        # of failing on the same cached bar forever.
        previous = {bar.open_time: bar for bar in existing}
        if any(previous.get(bar.open_time, bar) != bar for bar in fresh):
            self._bars.pop(key, None)
            raise ValueError("Exchange revised a previously closed candle")
        self._bars[key] = result
        return result

    async def range_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed candles fully inside [start, end]; used for bounded ambiguity replay."""
        payload = await self._get(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": timeframe,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": 99,
            },
        )
        return [
            candle
            for candle in (
                Candle(
                    open_time=timestamp(r[0]),
                    close_time=timestamp(r[6]),
                    open=r[1],
                    high=r[2],
                    low=r[3],
                    close=r[4],
                    volume=r[5],
                )
                for r in payload
            )
            if candle.open_time >= start and candle.close_time <= end
        ]

    async def _funding(self, symbol: str) -> dict[str, Any]:
        # One weight-10 request serves every symbol instead of one request per market.
        rows = await self._cached(
            "/fapi/v1/premiumIndex", self.settings.scanner_derivatives_cache_seconds
        )
        for row in rows:
            if row.get("symbol") == symbol:
                return row
        raise KeyError(symbol)

    async def derivatives(self, symbol: str, now: datetime) -> Derivatives:
        result = Derivatives()
        requests = [
            self._funding(symbol),
            self._cached(
                "/fapi/v1/openInterest",
                self.settings.scanner_derivatives_cache_seconds,
                {"symbol": symbol},
            ),
            self._cached(
                "/futures/data/openInterestHist",
                self.settings.scanner_derivatives_cache_seconds,
                {
                    "symbol": symbol,
                    "period": "15m",
                    "limit": 6,
                    "endTime": int(now.timestamp() // 900 * 900 * 1000),
                },
            ),
        ]
        responses = await asyncio.gather(*requests, return_exceptions=True)
        for name, data in zip(("funding", "oi", "oi_history"), responses):
            if isinstance(data, asyncio.CancelledError):
                raise data
            if isinstance(data, BaseException):
                result.errors.append(name + ": " + type(data).__name__)
                logger.warning(
                    "Derivative metric unavailable symbol=%s metric=%s error=%s",
                    symbol,
                    name,
                    type(data).__name__,
                )
                continue
            try:
                if name == "funding":
                    result.funding_rate = Decimal(data["lastFundingRate"])
                    result.funding_timestamp = timestamp(data["time"])
                elif name == "oi":
                    result.open_interest = Decimal(data["openInterest"])
                    result.oi_timestamp = timestamp(data["time"])
                else:
                    result.history = sorted(
                        [
                            OIPoint(
                                timestamp=timestamp(r["timestamp"]),
                                contracts=r["sumOpenInterest"],
                            )
                            for r in data
                            if timestamp(r["timestamp"]) <= now
                        ],
                        key=lambda p: p.timestamp,
                    )
            except (KeyError, ValueError, TypeError, ArithmeticError):
                result.errors.append(name + ": malformed data")
                logger.warning(
                    "Malformed derivative metric symbol=%s metric=%s", symbol, name
                )
        return result
