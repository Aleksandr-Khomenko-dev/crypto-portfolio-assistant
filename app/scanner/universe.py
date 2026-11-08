from __future__ import annotations

import re
from decimal import Decimal

from app.config import Settings
from app.scanner.domain import Contract, Ticker

# Binance lists gold as contractType TRADIFI_PERPETUAL (XAUUSDT, verified 2026-11-03).
EXPLICIT_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
# BingX TradFi perpetuals: NC + class (CO commodity, FX, SK stock, SI index) + BASE2QUOTE.
TRADFI_NAME = re.compile(r"NC(?:CO|FX|SK|SI)([A-Z0-9]+?)2([A-Z]{3})USDT")
DISPLAY_NAMES = {
    "NCCOGOLD2USDUSDT": "GOLD (XAU/USD)",
    "XAUUSDT": "GOLD (XAU/USD)",
    "NCCOXAG2USDUSDT": "SILVER (XAG/USD)",
    "NCCOXPT2USDUSDT": "PLATINUM (XPT/USD)",
    "NCCOPALLADIUM2USDUSDT": "PALLADIUM (XPD/USD)",
    "NCCO724COPPER2USDUSDT": "COPPER",
    "NCCO1OILWTI2USDUSDT": "OIL WTI",
    "NCCO1OILBRENT2USDUSDT": "OIL BRENT",
    "NCCO7241NATGAS2USDUSDT": "NATURAL GAS",
    "NCSINASDAQ1002USDUSDT": "NASDAQ 100",
    "NCSISP5002USDUSDT": "S&P 500",
}


def symbols(raw: str) -> set[str]:
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def display_symbol(symbol: str) -> str:
    """Human name for messages: NCCOGOLD2USDUSDT -> GOLD (XAU/USD); crypto unchanged."""
    if symbol in DISPLAY_NAMES:
        return DISPLAY_NAMES[symbol]
    match = TRADFI_NAME.fullmatch(symbol)
    return f"{match.group(1)}/{match.group(2)}" if match else symbol


def filter_universe(
    contracts: list[Contract], tickers: dict[str, Ticker], settings: Settings
) -> list[str]:
    """Crypto: the top-N USDT perpetuals by volume above the volume floor. TradFi
    markets named in scanner_tradfi_symbols are added after them, always (no cap,
    no volume floor): the operator chose them, and their prices are quoted."""
    whitelist, blacklist = (
        symbols(settings.scanner_whitelist),
        symbols(settings.scanner_blacklist),
    )
    always = symbols(settings.scanner_tradfi_symbols)
    eligible, added = [], []
    bases = {contract.base_asset for contract in contracts}
    for contract in contracts:
        symbol = contract.symbol
        # Naming a TradFi perpetual (e.g. BingX gold NCCOGOLD2USDUSDT, Binance XAUUSDT)
        # in the whitelist or the TradFi list is the explicit opt-in; without it only
        # crypto is scanned.
        explicit = symbol in whitelist or symbol in always
        if (
            contract.quote_asset != "USDT"
            or contract.contract_type
            not in (EXPLICIT_CONTRACT_TYPES if explicit else {"PERPETUAL"})
            or contract.status != "TRADING"
        ):
            continue
        if not explicit and (
            contract.underlying_type != "COIN"
            or any(
                s.upper() in {"STOCK", "COMMODITY", "TRADFI", "INDEX"}
                for s in contract.underlying_subtypes
            )
        ):
            continue
        if symbol in blacklist or (whitelist and symbol not in whitelist):
            continue
        # Numeric multipliers such as 1000SHIB are not leveraged tokens.
        leveraged = re.fullmatch(
            r"(.+?)(UP|DOWN|BULL|BEAR|[235]L|[235]S)", contract.base_asset
        )
        if settings.scanner_exclude_leveraged and (
            "LEVERAGED" in contract.underlying_subtypes
            or (leveraged is not None and leveraged.group(1) in bases)
        ):
            continue
        ticker = tickers.get(symbol)
        if ticker is None:
            continue
        if symbol in always:
            added.append(symbol)
        elif ticker.quote_volume >= settings.scanner_min_quote_volume_usd:
            eligible.append(symbol)

    def by_volume(symbol: str) -> tuple[Decimal, str]:
        return (-tickers[symbol].quote_volume, symbol)

    count = min(
        settings.scanner_top_n or settings.scanner_max_markets,
        settings.scanner_max_markets,
    )
    return sorted(eligible, key=by_volume)[:count] + sorted(added, key=by_volume)
