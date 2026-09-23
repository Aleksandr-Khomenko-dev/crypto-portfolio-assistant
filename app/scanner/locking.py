"""Crash-released scan ownership without holding portfolio database write locks."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

_MEMORY_LOCKS: dict[Engine, threading.Lock] = {}
_LOCK_KEY = 0x435044415343414E


@contextmanager
def database_scan_lock(engine: Engine) -> Iterator[bool]:
    if engine.dialect.name == "postgresql":
        # A session advisory lock must remain on this exact connection across commits.
        with engine.connect() as connection:
            acquired = bool(
                connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
                )
            )
            connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
                    )
                    connection.commit()
    elif engine.dialect.name == "sqlite":
        database = engine.url.database
        if not database or database == ":memory:":
            lock = _MEMORY_LOCKS.setdefault(engine, threading.Lock())
            acquired = lock.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
        else:
            import fcntl

            path = Path(database).resolve()
            with path.with_name(path.name + ".scanner.lock").open("a") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    yield False
                else:
                    try:
                        yield True
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        raise ValueError("Scanner locking supports PostgreSQL and SQLite")
