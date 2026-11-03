"""Trading hours of the real market behind a TradFi perpetual.

BingX quotes its gold perpetual 24/7, but the underlying gold market (CME Globex /
spot XAU) is closed from Friday 17:00 to Sunday 18:00 New York time and pauses
daily 17:00-18:00. Candles in those windows are thin exchange-internal prints
(observed 2026-10-31/11-01: ~1/6 of weekday volume, ~1/5 of the hourly range), so
setups and fast-move alerts formed there are not about the gold market at all.

Exchange holidays are not modelled.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
CLOSE_MINUTE = 17 * 60  # 17:00 New York: Friday close and daily break start
REOPEN_MINUTE = 18 * 60  # 18:00 New York: Sunday reopen and daily break end


def metals_market_open(at: datetime) -> bool:
    """True while the CME metals session is open. ``at`` must be timezone-aware."""
    local = at.astimezone(NEW_YORK)
    minute = local.hour * 60 + local.minute
    weekday = local.weekday()  # Monday = 0 ... Sunday = 6
    if weekday == 5:
        return False
    if weekday == 6:
        return minute >= REOPEN_MINUTE
    if weekday == 4 and minute >= CLOSE_MINUTE:
        return False
    return not CLOSE_MINUTE <= minute < REOPEN_MINUTE


def follows_market_hours(underlying_type: str) -> bool:
    """Crypto trades 24/7; every other underlying is gated by real-market hours."""
    return underlying_type != "COIN"
