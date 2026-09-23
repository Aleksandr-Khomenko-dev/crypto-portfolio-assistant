from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.scanner.domain import Candle, Derivatives


def funding_8h_equivalent(rate: Decimal, interval_hours: int | None) -> Decimal:
    """`extreme` is defined per 8h; a 1h contract's 0.01% is 0.08% per 8h.

    An unknown interval keeps the rate as reported (the historical behaviour)."""
    return rate * 8 / interval_hours if interval_hours else rate


def normalize_derivatives(
    data: Derivatives, bars: list[Candle], extreme: Decimal, now: datetime | None = None
) -> Derivatives:
    result = data.model_copy(deep=True)
    result.oi_change_pct = result.price_change_pct = None
    result.interpretation = result.funding_state = "unavailable"
    if now is not None:
        for metric, timestamp in (
            ("funding_rate", data.funding_timestamp),
            ("open_interest", data.oi_timestamp),
        ):
            if (
                timestamp is None
                or not -60 <= (now - timestamp).total_seconds() <= 1800
            ):
                if getattr(result, metric) is not None:
                    result.errors.append(metric + ": stale or missing timestamp")
                setattr(result, metric, None)
        data = result

    if data.funding_rate is not None and data.funding_rate.is_finite():
        rate = funding_8h_equivalent(data.funding_rate, data.funding_interval_hours)
        result.funding_state = (
            "CROWDED_LONG"
            if rate >= extreme
            else ("CROWDED_SHORT" if rate <= -extreme else "NEUTRAL")
        )
    # Pair OI and price over the same completed 15m interval. Never mix current OI with an old close.
    closes = {b.open_time + timedelta(minutes=15): b.close for b in bars}
    matched = sorted(
        {
            p.timestamp: p
            for p in data.history
            if p.timestamp in closes and (now is None or p.timestamp <= now)
        }.values(),
        key=lambda p: p.timestamp,
    )
    if len(matched) >= 2 and (
        now is None or (now - matched[-1].timestamp).total_seconds() <= 1800
    ):
        first, last = matched[-2:]
        if (
            last.timestamp - first.timestamp == timedelta(minutes=15)
            and first.contracts > 0
        ):
            result.oi_change_pct = float((last.contracts / first.contracts - 1) * 100)
            result.price_change_pct = float(
                (closes[last.timestamp] / closes[first.timestamp] - 1) * 100
            )
            oi, price = result.oi_change_pct, result.price_change_pct
            result.interpretation = (
                "NEW_LONG_PARTICIPATION_POSSIBLE"
                if price > 0 and oi > 0
                else "SHORT_COVERING_POSSIBLE"
                if price > 0 and oi < 0
                else "SHORT_BUILD_POSSIBLE"
                if price < 0 and oi > 0
                else "DELEVERAGING_POSSIBLE"
                if price < 0 and oi < 0
                else "MIXED"
            )
    return result
