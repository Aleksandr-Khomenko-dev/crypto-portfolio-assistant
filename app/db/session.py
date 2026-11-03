from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_ENGINE_CACHE: dict[str, Engine] = {}


def _build_connect_args(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        # timeout: seconds to wait on "database is locked" before failing
        return {"check_same_thread": False, "timeout": 30}
    return {}


def _sqlite_wal(dbapi_connection, _record) -> None:
    """WAL lets the news, fast-watcher and scanner writers work while the scanner runs
    a long read; in the default rollback journal one minute-long read (first-cycle
    history seeding on a large database) made concurrent writes fail with
    "database is locked" after the 30 s timeout. The setting is persistent per file;
    in-memory databases keep their own mode."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def _build_engine(database_url: str) -> Engine:
    engine = _ENGINE_CACHE.get(database_url)
    if engine is None:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            future=True,
            connect_args=_build_connect_args(database_url),
        )
        if database_url.startswith("sqlite"):
            event.listen(engine, "connect", _sqlite_wal)
        _ENGINE_CACHE[database_url] = engine
    return engine


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    return _build_engine(url)


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(database_url),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_db() -> Generator[Session, None, None]:
    session_factory = get_session_factory()
    with session_factory() as session:
        yield session


def get_db_session(database_url: str | None = None) -> Session:
    return get_session_factory(database_url)()


def reset_engine_cache() -> None:
    for engine in _ENGINE_CACHE.values():
        engine.dispose()
    _ENGINE_CACHE.clear()
