from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    from app.services.scanner_service import ScannerRuntime
from app.providers.cached import CachedMarketDataProvider
from app.providers.factory import create_market_provider
from app.providers.interfaces import AbstractMarketDataProvider

# Singleton cache shared across all requests for the lifetime of the process.
_provider_cache: CachedMarketDataProvider | None = None


def get_cached_provider() -> CachedMarketDataProvider:
    global _provider_cache
    if _provider_cache is None:
        settings = get_settings()
        inner = create_market_provider(settings)
        _provider_cache = CachedMarketDataProvider(
            inner, ttl_seconds=settings.quote_cache_ttl_seconds
        )
    return _provider_cache


async def get_market_provider() -> AbstractMarketDataProvider:
    return get_cached_provider()


async def close_cached_provider() -> None:
    global _provider_cache
    if _provider_cache is not None:
        await _provider_cache.aclose()
        _provider_cache = None


_scanner_runtime: ScannerRuntime | None = None
_scanner_runtime_guard = Lock()


def get_scanner_runtime() -> ScannerRuntime:
    from app.providers.futures_factory import create_futures_provider
    from app.services.scanner_service import ScannerRuntime

    global _scanner_runtime
    with _scanner_runtime_guard:
        if _scanner_runtime is None:
            settings = get_settings()
            _scanner_runtime = ScannerRuntime(
                create_futures_provider(settings), settings
            )
        return _scanner_runtime


async def close_scanner_runtime() -> None:
    global _scanner_runtime
    if _scanner_runtime is not None:
        await _scanner_runtime.aclose()
        _scanner_runtime = None
