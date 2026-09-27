"""BingX USDT-M perpetual swap public market data (read-only, unauthenticated).

Verified against the live API (2026-09-23):

* All endpoints used here are public; no API key or signature is required.
* Responses are HTTP 200 with ``{"code": 0, "data": ...}``; a non-zero ``code`` is an
  error (e.g. 109425 unknown symbol, 109400 invalid parameter).
* v3 klines return objects ``{open, high, low, close, volume, time}`` newest-first,
  where ``time`` is the OPEN time. There is no close time and the first row is the
  still-forming candle. ``endTime`` is not a reliable closed-candle bound, so closed
  candles are selected here by computed close time.
* ``openInterest`` is USDT notional (BTC ~9e8 at ~84k price), not base quantity.
* ``premiumIndex`` gives the current funding rate, next funding time and the
  per-contract funding interval (1h, 4h and 8h all occur).
* Rate-limit headers: ``x-ratelimit-requests-remain`` / ``x-ratelimit-requests-expire``
  (ms); observed 500 requests per 10 s window.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from app.analytics.open_interest import OIObservation, aligned_bucket
from app.config import Settings
from app.providers.request_control import RequestBudget
from app.scanner.domain import (
    INTERVAL_SECONDS,
    Candle,
    Contract,
    Derivatives,
    Ticker,
    Timeframe,
)

logger = logging.getLogger(__name__)

QUOTE = "USDT"
# BingX TradFi perpetuals: stocks, FX, commodities and indices (e.g. NCSKTSLA2USD-USDT).
TRADFI_PREFIXES = ("NCSK", "NCFX", "NCCO", "NCSI")
# Codes BingX documents as request-frequency limits; treated like HTTP 429.
RATE_LIMIT_CODES = frozenset({100410})
RETRYABLE_STATUS = frozenset({418, 429, 500, 502, 503, 504})


class BingXAPIError(RuntimeError):
    """BingX returned an error envelope or a malformed body. Not retried."""


class _RateLimited(RuntimeError):
    pass


def internal_to_bingx_symbol(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC-USDT``. Only USDT-quoted canonical symbols exist here."""
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol) or symbol == QUOTE:
        raise ValueError(f"Not a canonical USDT symbol: {symbol!r}")
    return symbol.removesuffix(QUOTE) + "-" + QUOTE


def bingx_to_internal_symbol(symbol: str) -> str:
    """``BTC-USDT`` -> ``BTCUSDT``."""
    base, separator, quote = symbol.partition("-")
    if not separator or not base or quote != QUOTE or "-" in base:
        raise ValueError(f"Not a BingX USDT perpetual symbol: {symbol!r}")
    return base + quote


def timestamp(milliseconds: int | str) -> datetime:
    return datetime.fromtimestamp(int(milliseconds) / 1000, UTC)


def parse_candle(row: dict[str, Any], timeframe: Timeframe) -> Candle:
    opened = timestamp(row["time"])
    return Candle(
        open_time=opened,
        # Same convention as the rest of the app: close = next open - 1 ms.
        close_time=opened
        + timedelta(seconds=INTERVAL_SECONDS[timeframe], milliseconds=-1),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
    )


def parse_contract(row: dict[str, Any]) -> Contract:
    symbol = row["symbol"]
    base, _, quote = symbol.partition("-")
    tradable = str(row.get("status")) == "1" and str(row.get("apiStateOpen")) == "true"
    return Contract(
        symbol=bingx_to_internal_symbol(symbol) if quote == QUOTE else base + quote,
        base_asset=row.get("asset") or base,
        quote_asset=row.get("currency") or quote,
        contract_type="PERPETUAL",
        status="TRADING" if tradable else "NOT_TRADING",
        underlying_type="TRADFI" if symbol.startswith(TRADFI_PREFIXES) else "COIN",
    )


class BingXFuturesProvider:
    """Public BingX swap client with one shared limiter and incremental closed-bar cache."""

    exchange = "BINGX"
    publishes_oi_history = False  # history comes from stored live observations

    def __init__(
        self, settings: Settings, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            base_url=settings.bingx_base_url,
            timeout=settings.http_timeout_seconds,
            limits=httpx.Limits(max_connections=settings.scanner_concurrency),
        )
        self._semaphore = asyncio.Semaphore(settings.scanner_concurrency)
        self._budget = RequestBudget(
            settings.scanner_requests_per_second,
            settings.bingx_requests_per_window,
            window_seconds=settings.bingx_rate_window_seconds,
        )
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._bar_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cache: dict[str, tuple[float, Any, datetime]] = {}
        self._bars: dict[tuple[str, str], list[Candle]] = {}
        self.stats: Counter[str] = Counter()
        self._normal_requests = asyncio.Event()
        self._normal_requests.set()
        self._oi_priority: ContextVar[bool] = ContextVar(
            "bingx_oi_priority", default=False
        )
        self._oi_priority_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _cooldown(self, response: httpx.Response, fallback: float) -> float:
        for header, scale in (
            ("Retry-After", 1.0),
            ("x-ratelimit-requests-expire", 0.001),
        ):
            try:
                return max(1.0, float(response.headers[header]) * scale)
            except (KeyError, ValueError):
                continue
        return fallback

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        retries = self.settings.scanner_http_retries
        for attempt in range(retries + 1):
            # OI gets a short priority window, but consumes exactly the same rate budget.
            # Recheck after acquiring the semaphore: scan requests may already be queued.
            while True:
                if not self._oi_priority.get():
                    await self._normal_requests.wait()
                await self._semaphore.acquire()
                if self._oi_priority.get() or self._normal_requests.is_set():
                    break
                self._semaphore.release()
            try:
                await self._budget.acquire(1)
                try:
                    self.stats["requests"] += 1
                    response = await self.client.get(path, params=params)
                    remain = response.headers.get("x-ratelimit-requests-remain", "")
                    if remain.isdigit() and int(remain) <= 10:
                        # Nearly exhausted window: pause every caller until it resets.
                        self._budget.defer(self._cooldown(response, 10))
                    if response.status_code in (418, 429):
                        self.stats[f"http_{response.status_code}"] += 1
                        self._budget.defer(
                            self._cooldown(
                                response, 10 if response.status_code == 429 else 60
                            )
                        )
                    response.raise_for_status()
                    return self._unwrap(response)
                except httpx.HTTPStatusError as exc:
                    if (
                        exc.response.status_code not in RETRYABLE_STATUS
                        or attempt == retries
                    ):
                        raise
                except (httpx.TransportError, _RateLimited):
                    if attempt == retries:
                        raise
            finally:
                self._semaphore.release()
            logger.warning(
                "BingX transient request failure: %s attempt=%s", path, attempt + 1
            )
            await asyncio.sleep(2**attempt)
        raise RuntimeError("Unreachable retry state")

    def _unwrap(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BingXAPIError("Malformed BingX response body") from exc
        if not isinstance(payload, dict) or "code" not in payload:
            raise BingXAPIError("Malformed BingX response envelope")
        code = payload["code"]
        if code == 0:
            return payload.get("data")
        if code in RATE_LIMIT_CODES:
            self.stats["rate_limit_codes"] += 1
            self._budget.defer(10)
            raise _RateLimited(f"BingX rate limit code {code}")
        # The message names the parameter or symbol; it never contains credentials.
        raise BingXAPIError(f"BingX error {code}: {str(payload.get('msg'))[:120]}")

    async def _cached(
        self,
        path: str,
        ttl: float,
        params: dict[str, Any] | None = None,
        *,
        not_before: datetime | None = None,
    ) -> tuple[Any, datetime]:
        """Returns data and the wall-clock time it was fetched (for freshness)."""
        key = path + repr(sorted((params or {}).items()))
        if not self._oi_priority.get():
            await self._normal_requests.wait()
        async with self._cache_locks.setdefault(key, asyncio.Lock()):
            cached = self._cache.get(key)
            if (
                cached
                # Wall clock: monotonic time stops during OS sleep, which kept a
                # pre-sleep server time 'fresh' after wake (all markets stale).
                and cached[0] > time.time()
                and (not_before is None or cached[2] >= not_before)
            ):
                self.stats["cache_hits"] += 1
                return cached[1], cached[2]
            self.stats["cache_misses"] += 1
            token = self._oi_priority.set(True)
            try:
                data = await self._get(path, params)
            finally:
                self._oi_priority.reset(token)
            now = time.time()
            for stale in [k for k, entry in self._cache.items() if entry[0] <= now]:
                if not self._cache_locks[stale].locked():
                    del self._cache[stale], self._cache_locks[stale]
            fetched_at = datetime.now(UTC)
            self._cache[key] = (now + ttl, data, fetched_at)
            return data, fetched_at

    async def contracts(self) -> list[Contract]:
        data, _ = await self._cached("/openApi/swap/v2/quote/contracts", 3600)
        result = []
        for row in data or []:
            try:
                result.append(parse_contract(row))
            except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
                logger.warning("Ignoring malformed BingX contract metadata")
        return result

    async def tickers(self) -> dict[str, Ticker]:
        data, _ = await self._cached(
            "/openApi/swap/v2/quote/ticker",
            self.settings.scanner_universe_cache_seconds,
        )
        result = {}
        for row in data or []:
            try:
                if not str(row["symbol"]).endswith("-" + QUOTE):
                    continue
                ticker = Ticker(
                    symbol=bingx_to_internal_symbol(row["symbol"]),
                    price=row["lastPrice"],
                    quote_volume=row["quoteVolume"],  # 24h USDT turnover
                    timestamp=timestamp(row["closeTime"]),
                    bid=row.get("bidPrice") or None,
                    ask=row.get("askPrice") or None,
                )
                result[ticker.symbol] = ticker
            except (KeyError, TypeError, ValueError, ValidationError):
                logger.warning("Ignoring malformed BingX ticker")
        return result

    async def _server_now(self, now: datetime) -> datetime:
        data, _ = await self._cached("/openApi/swap/v2/server/time", 30)
        return min(now, timestamp(data["serverTime"]))

    async def candles(
        self, symbol: str, timeframe: Timeframe, now: datetime
    ) -> list[Candle]:
        async with self._bar_locks.setdefault((symbol, timeframe), asyncio.Lock()):
            return await self._candles(symbol, timeframe, now)

    async def _candles(
        self, symbol: str, timeframe: Timeframe, now: datetime
    ) -> list[Candle]:
        interval = INTERVAL_SECONDS[timeframe]
        now = await self._server_now(now)
        # Last candle closed at least `grace` seconds ago (never the forming one).
        boundary = int(
            (now.timestamp() - self.settings.scanner_data_grace_seconds)
            // interval
            * interval
        )
        end = timestamp(boundary * 1000) - timedelta(milliseconds=1)
        key = (symbol, timeframe)
        existing = self._bars.get(key, [])
        if existing and existing[-1].close_time >= end:
            self.stats["candle_cache_hits"] += 1
            return [bar for bar in existing if bar.close_time <= end]
        limit = self.settings.scanner_candle_limit
        params: dict[str, Any] = {
            "symbol": internal_to_bingx_symbol(symbol),
            "interval": timeframe,
            "limit": limit,
            "endTime": int(end.timestamp() * 1000),
        }
        if existing and (end - existing[-1].close_time).total_seconds() < interval * (
            limit - 2
        ):
            params["startTime"] = int(existing[-1].open_time.timestamp() * 1000)
        else:
            existing = []
        self.stats["candle_cache_misses"] += 1
        payload = await self._get("/openApi/swap/v3/quote/klines", params)
        if not isinstance(payload, list):
            raise BingXAPIError("Malformed BingX klines payload")
        fresh = [
            bar
            for bar in (parse_candle(row, timeframe) for row in payload)
            if bar.close_time <= end
        ]
        previous = {bar.open_time: bar for bar in existing}
        if any(previous.get(bar.open_time, bar) != bar for bar in fresh):
            self._bars.pop(key, None)
            raise ValueError("Exchange revised a previously closed candle")
        merged = {bar.open_time: bar for bar in existing + fresh}
        result = sorted(merged.values(), key=lambda bar: bar.open_time)[-limit:]
        self._bars[key] = result
        return result

    async def range_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        limit: int = 20,
    ) -> list[Candle]:
        payload = await self._get(
            "/openApi/swap/v3/quote/klines",
            {
                "symbol": internal_to_bingx_symbol(symbol),
                "interval": timeframe,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": limit,
            },
        )
        bars = [parse_candle(row, timeframe) for row in payload or []]
        return sorted(
            (b for b in bars if b.open_time >= start and b.close_time <= end),
            key=lambda b: b.open_time,
        )

    async def open_interest_notional(self, symbol: str) -> Decimal:
        """Current USDT-notional OI for one symbol (1 s cache; shared limiter)."""
        data, _ = await self._cached(
            "/openApi/swap/v2/quote/openInterest",
            1,
            {"symbol": internal_to_bingx_symbol(symbol)},
        )
        return Decimal(data["openInterest"])

    async def announcements(self, content_type: str) -> list[dict[str, Any]]:
        """Official BingX notices (public, verified live). Shares this provider's limiter."""
        data = await self._get(
            "/openApi/content/v1/announcement", {"contentType": content_type}
        )
        rows = data.get("list") if isinstance(data, dict) else data
        return [row for row in rows or [] if isinstance(row, dict)]

    async def _funding(self, symbol: str) -> tuple[dict[str, Any], datetime]:
        # One bulk request covers every contract's current funding.
        rows, fetched_at = await self._cached(
            "/openApi/swap/v2/quote/premiumIndex",
            self.settings.scanner_derivatives_cache_seconds,
        )
        wanted = internal_to_bingx_symbol(symbol)
        for row in rows or []:
            if row.get("symbol") == wanted:
                return row, fetched_at
        raise KeyError(symbol)

    @asynccontextmanager
    async def oi_collection_priority(self):
        async with self._oi_priority_lock:
            token = self._oi_priority.set(True)
            self._normal_requests.clear()
            try:
                yield
            finally:
                self._normal_requests.set()
                self._oi_priority.reset(token)

    async def oi_mark_prices(self, boundary: datetime) -> dict[str, Decimal]:
        from app.providers.bingx_mark_prices import collect_mark_prices
        from app.scanner.universe import filter_universe

        # Universe endpoints are already warm from the collector (single-flight cache).
        universe = filter_universe(
            await self.contracts(), await self.tickers(), self.settings
        )
        prices = await collect_mark_prices(
            self.settings.bingx_mark_price_ws_url,
            [internal_to_bingx_symbol(symbol) for symbol in universe],
            boundary,
            self._budget,
            self.settings.http_timeout_seconds,
        )
        return {
            bingx_to_internal_symbol(symbol): price for symbol, price in prices.items()
        }

    async def oi_snapshot(
        self, symbol: str, boundary: datetime, mark: Decimal
    ) -> OIObservation:
        if not mark.is_finite() or mark <= 0:
            raise ValueError("Invalid OI conversion mark price")
        row, _ = await self._cached(
            "/openApi/swap/v2/quote/openInterest",
            self.settings.scanner_derivatives_cache_seconds,
            {"symbol": internal_to_bingx_symbol(symbol)},
            not_before=boundary,
        )
        notional = Decimal(row["openInterest"])
        observed = timestamp(row["time"])
        bucket = aligned_bucket(observed, self.settings.oi_snapshot_tolerance_seconds)
        # Return the real timestamp even when unaligned, so the collector can diagnose it.
        if not notional.is_finite() or notional <= 0:
            raise ValueError("Invalid OI value")
        return OIObservation(bucket or observed, observed, notional / mark, notional)

    async def funding(self, symbol: str, now: datetime) -> Derivatives:
        """Scanner path: funding only. Stored OI is attached by ScannerService."""
        result = Derivatives()
        try:
            row, fetched_at = await self._funding(symbol)
            result.funding_rate = Decimal(row["lastFundingRate"])
            result.funding_timestamp = fetched_at
            result.next_funding_at = timestamp(row["nextFundingTime"])
            hours = row.get("fundingIntervalHours")
            result.funding_interval_hours = int(hours) if hours else None
        except Exception as exc:  # noqa: BLE001 - optional metric failure
            result.errors.append("funding: " + type(exc).__name__)
        return result

    async def derivatives(self, symbol: str, now: datetime) -> Derivatives:
        """Public compatibility path. The scanner uses funding() plus stored OI."""
        result = await self.funding(symbol, now)
        try:
            row, _ = await self._cached(
                "/openApi/swap/v2/quote/openInterest",
                self.settings.scanner_derivatives_cache_seconds,
                {"symbol": internal_to_bingx_symbol(symbol)},
            )
            result.open_interest_notional = Decimal(row["openInterest"])
            result.oi_timestamp = timestamp(row["time"])
            mark_row, _ = await self._funding(symbol)
            mark = Decimal(mark_row["markPrice"])
            if mark > 0:
                result.open_interest = result.open_interest_notional / mark
        except Exception as exc:  # noqa: BLE001 - optional metric failure
            result.errors.append("oi: " + type(exc).__name__)
        return result
