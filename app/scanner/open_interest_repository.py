"""Persistence for self-recorded open interest (exchange-specific, restart-safe)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.analytics.open_interest import OIObservation
from app.db.scanner_models import OpenInterestSnapshot


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _observation(row: OpenInterestSnapshot) -> OIObservation:
    return OIObservation(
        _utc(row.bucket_at),
        _utc(row.observed_at),
        row.open_interest,
        row.open_interest_notional,
    )


def load_history(
    session: Session, exchange: str, symbols: Iterable[str], since: datetime
) -> dict[str, dict[datetime, OIObservation]]:
    history: dict[str, dict[datetime, OIObservation]] = {}
    rows = session.scalars(
        select(OpenInterestSnapshot).where(
            OpenInterestSnapshot.exchange == exchange,
            OpenInterestSnapshot.symbol.in_(list(symbols)),
            OpenInterestSnapshot.bucket_at >= since,
        )
    )
    for row in rows:
        observation = _observation(row)
        history.setdefault(row.symbol, {})[observation.bucket_at] = observation
    return history


def record(
    session: Session,
    exchange: str,
    symbol: str,
    observation: OIObservation,
    now: datetime,
) -> str:
    """Idempotent upsert per (exchange, symbol, bucket). Returns the action taken."""
    dialect = session.get_bind().dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    values = {
        "exchange": exchange,
        "symbol": symbol,
        "bucket_at": observation.bucket_at,
        "observed_at": observation.observed_at,
        "open_interest": observation.open_interest,
        "open_interest_notional": observation.open_interest_notional,
        "created_at": now,
    }
    # First operation is a write: no SQLite read -> write upgrade race. Both statements
    # run in the caller's short transaction; the unique constraint arbitrates workers.
    inserted = session.scalar(
        insert(OpenInterestSnapshot)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["exchange", "symbol", "bucket_at"])
        .returning(OpenInterestSnapshot.id)
    )
    if inserted is not None:
        return "inserted"
    row = OpenInterestSnapshot
    # Compare timestamps, not floating epoch offsets (SQLite julianday loses precision).
    low = observation.bucket_at - abs(observation.observed_at - observation.bucket_at)
    high = observation.bucket_at + abs(observation.observed_at - observation.bucket_at)
    changed = session.scalar(
        update(row)
        .where(
            row.exchange == exchange,
            row.symbol == symbol,
            row.bucket_at == observation.bucket_at,
            (row.observed_at < low) | (row.observed_at > high),
        )
        .values(
            observed_at=observation.observed_at,
            open_interest=observation.open_interest,
            open_interest_notional=observation.open_interest_notional,
        )
        .returning(row.id)
    )
    return "replaced" if changed is not None else "kept"


def prune(session: Session, exchange: str, before: datetime) -> int:
    result = session.execute(
        delete(OpenInterestSnapshot).where(
            OpenInterestSnapshot.exchange == exchange,
            OpenInterestSnapshot.bucket_at < before,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)
