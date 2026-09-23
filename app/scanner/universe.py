from __future__ import annotations

import re

from app.config import Settings
from app.scanner.domain import Contract, Ticker


def symbols(raw: str) -> set[str]:
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def filter_universe(
    contracts: list[Contract], tickers: dict[str, Ticker], settings: Settings
) -> list[str]:
    whitelist, blacklist = (
        symbols(settings.scanner_whitelist),
        symbols(settings.scanner_blacklist),
    )
    eligible = []
    bases = {contract.base_asset for contract in contracts}
    for contract in contracts:
        symbol = contract.symbol
        if (
            contract.quote_asset != "USDT"
            or contract.contract_type != "PERPETUAL"
            or contract.status != "TRADING"
        ):
            continue
        if contract.underlying_type != "COIN" or any(
            s.upper() in {"STOCK", "COMMODITY", "TRADFI", "INDEX"}
            for s in contract.underlying_subtypes
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
        if (
            ticker is None
            or ticker.quote_volume < settings.scanner_min_quote_volume_usd
        ):
            continue
        eligible.append(symbol)
    eligible.sort(key=lambda symbol: (-tickers[symbol].quote_volume, symbol))
    count = min(
        settings.scanner_top_n or settings.scanner_max_markets,
        settings.scanner_max_markets,
    )
    return eligible[:count]
