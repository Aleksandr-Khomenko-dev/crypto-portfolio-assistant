"""Bounded scanner history (CPDA_SCANNER_SNAPSHOT_RETENTION_DAYS).

Every cycle stores one ~120 KB snapshot per market, and every run keeps the BTC/ETH
context frames (~190 KB). With dozens of markets that is gigabytes a day. Snapshots
and runs older than the retention window are deleted in small batches, except:

* snapshots a setup (`market_setups.snapshot_id`) or a research outcome
  (`setup_outcomes.ready_snapshot_id`) refers to;
* runs that still have snapshots, and the latest run (its context frames seed the
  next start).

SQLite reuses the freed pages, so the file stops growing; it does not shrink.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.scanner_models import MarketSetup, ScannerRun, ScannerSnapshot, SetupOutcome

BATCH = 200  # ~24 MB of snapshots per transaction: short write locks


def prune_batch(
    sessions: sessionmaker[Session], cutoff: datetime, batch: int = BATCH
) -> int:
    """Delete up to `batch` old snapshots; when none are left, up to `batch` old
    runs without snapshots. Returns the rows deleted (0 = nothing left)."""
    with sessions() as session:
        snapshots = list(
            session.scalars(
                select(ScannerSnapshot.id)
                .where(
                    ScannerSnapshot.created_at < cutoff,
                    ScannerSnapshot.id.not_in(
                        select(MarketSetup.snapshot_id).where(
                            MarketSetup.snapshot_id.is_not(None)
                        )
                    ),
                    ScannerSnapshot.id.not_in(
                        select(SetupOutcome.ready_snapshot_id).where(
                            SetupOutcome.ready_snapshot_id.is_not(None)
                        )
                    ),
                )
                .limit(batch)
            )
        )
        if snapshots:
            session.execute(
                delete(ScannerSnapshot)
                .where(ScannerSnapshot.id.in_(snapshots))
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return len(snapshots)
        latest = session.scalar(
            select(ScannerRun.id).order_by(ScannerRun.started_at.desc()).limit(1)
        )
        runs = list(
            session.scalars(
                select(ScannerRun.id)
                .where(
                    ScannerRun.started_at < cutoff,
                    ScannerRun.id != latest,
                    ~exists().where(ScannerSnapshot.run_id == ScannerRun.id),
                )
                .limit(batch)
            )
        )
        if runs:
            session.execute(
                delete(ScannerRun)
                .where(ScannerRun.id.in_(runs))
                .execution_options(synchronize_session=False)
            )
            session.commit()
        return len(runs)


async def prune(
    sessions: sessionmaker[Session],
    now: datetime,
    days: int,
    stop: asyncio.Event | None = None,
    pause: float = 0.1,
) -> int:
    """Prune everything older than `days`, batch by batch in a worker thread (the
    event loop and other writers get a turn between batches)."""
    cutoff = now - timedelta(days=days)
    total = 0
    while stop is None or not stop.is_set():
        deleted = await asyncio.to_thread(prune_batch, sessions, cutoff)
        if not deleted:
            break
        total += deleted
        await asyncio.sleep(pause)
    return total
