"""Futures providers by exchange.

CPDA_SCANNER_PROVIDER picks the exchange for NEW scans only. Existing setups keep the
exchange they were created on; outcome tracking resolves its provider from the
registry by that stored exchange, so switching the scanner never re-routes them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from app.config import Settings
from app.providers.futures import FuturesProvider

EXCHANGES = ("BINGX", "BINANCE")


def provider_for_exchange(exchange: str, settings: Settings) -> FuturesProvider:
    if exchange == "BINGX":
        from app.providers.bingx_futures import BingXFuturesProvider

        return BingXFuturesProvider(settings)
    if exchange == "BINANCE":
        from app.providers.binance_futures import BinanceFuturesProvider

        return BinanceFuturesProvider(settings)
    raise ValueError(f"Unsupported futures exchange: {exchange}")


def create_futures_provider(settings: Settings) -> FuturesProvider:
    return provider_for_exchange(settings.scanner_provider.upper(), settings)


Factory = Callable[[str, Settings], FuturesProvider]


class ProviderRegistry:
    """One long-lived provider per exchange (keeps each exchange's candle cache)."""

    def __init__(
        self,
        settings: Settings,
        providers: dict[str, FuturesProvider] | None = None,
        factory: Factory | None = provider_for_exchange,
    ) -> None:
        self.settings = settings
        self._providers = dict(providers or {})
        self._factory = factory

    def register(self, provider: FuturesProvider) -> None:
        self._providers.setdefault(provider.exchange, provider)

    def get(self, exchange: str) -> FuturesProvider:
        """Raises LookupError when no provider exists or can be built for `exchange`."""
        provider = self._providers.get(exchange)
        if provider is None:
            if self._factory is None:
                raise LookupError(f"No provider registered for {exchange}")
            try:
                provider = self._factory(exchange, self.settings)
            except ValueError as exc:
                raise LookupError(str(exc)) from exc
            self._providers[exchange] = provider
        return provider

    def stats(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for provider in self._providers.values():
            total.update(getattr(provider, "stats", {}))
        return total

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
