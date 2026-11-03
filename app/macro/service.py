"""Cached gold macro context for scanner runs (one fetch per cache period, isolated
failures: a missing input is reported in the context, never fatal for the scan)."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

import httpx

from app.config import Settings
from app.macro import sources
from app.macro.context import gold_macro_context
from app.scanner.domain import Candle, MarketContext

logger = logging.getLogger(__name__)
VIX_QUOTE_SECONDS = 300  # the Cboe quote is itself ~15 minutes delayed


class GoldMacroService:
    def __init__(
        self,
        settings: Settings,
        provider,  # FuturesProvider of the scanning exchange (DXY candles)
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.client = client or sources.new_client(settings.http_timeout_seconds)
        self._us10y: tuple[float, list[tuple[date, float]]] | None = None
        self._vix: tuple[float, float, list[tuple[date, float]]] | None = None
        self.errors: dict[str, str] = {}

    async def _dxy(self, now: datetime) -> list[Candle] | None:
        try:
            return await self.provider.candles(
                self.settings.macro_dxy_symbol, "4h", now
            )
        except Exception as exc:  # noqa: BLE001 - macro input is optional
            self.errors["DXY"] = type(exc).__name__
            return None

    async def _yields(self, now: datetime) -> list[tuple[date, float]] | None:
        fresh = (
            self._us10y
            and time.monotonic() - self._us10y[0] < self.settings.macro_cache_seconds
        )
        if not fresh:
            try:
                rows = await sources.fetch_us10y(self.client, now.date())
                self._us10y = (time.monotonic(), rows)
                self.errors.pop("US10Y", None)
            except Exception as exc:  # noqa: BLE001
                self.errors["US10Y"] = type(exc).__name__
        return self._us10y[1] if self._us10y else None

    async def _vix_data(self) -> tuple[float | None, list[tuple[date, float]]]:
        fresh = self._vix and time.monotonic() - self._vix[0] < VIX_QUOTE_SECONDS
        if not fresh:
            try:
                current, history = await sources.fetch_vix(self.client)
                self._vix = (time.monotonic(), current, history)
                self.errors.pop("VIX", None)
            except Exception as exc:  # noqa: BLE001
                self.errors["VIX"] = type(exc).__name__
        if self._vix is None:
            return None, []
        return self._vix[1], self._vix[2]

    async def context(self, now: datetime) -> MarketContext:
        dxy = await self._dxy(now)
        if dxy is not None:
            self.errors.pop("DXY", None)
        us10y = await self._yields(now)
        vix_now, vix_history = await self._vix_data()
        context = gold_macro_context(dxy, us10y, vix_now, vix_history, self.settings)
        if self.errors:
            logger.warning("Gold macro inputs missing=%s", self.errors)
            context.assets["errors"] = dict(self.errors)
        return context

    async def aclose(self) -> None:
        await self.client.aclose()
