"""Select the scanner's single futures exchange from CPDA_SCANNER_PROVIDER."""

from __future__ import annotations

from app.config import Settings
from app.providers.futures import FuturesProvider


def create_futures_provider(settings: Settings) -> FuturesProvider:
    if settings.scanner_provider == "bingx":
        from app.providers.bingx_futures import BingXFuturesProvider

        return BingXFuturesProvider(settings)
    if settings.scanner_provider == "binance":
        from app.providers.binance_futures import BinanceFuturesProvider

        return BinanceFuturesProvider(settings)
    raise ValueError(f"Unsupported scanner provider: {settings.scanner_provider}")
