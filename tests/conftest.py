from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.routes import create_app
from app.config import clear_settings_cache, get_settings
from app.db.init_db import init_db
from app.db.session import get_session_factory, reset_engine_cache


@pytest.fixture()
def sqlite_database_url(tmp_path, monkeypatch) -> str:
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("CPDA_DATABASE_URL", database_url)
    monkeypatch.setenv("CPDA_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("CPDA_TELEGRAM_POLLING_ENABLED", "false")
    clear_settings_cache()
    reset_engine_cache()
    init_db(database_url)
    yield database_url
    clear_settings_cache()
    reset_engine_cache()


@pytest.fixture()
def session(sqlite_database_url):
    session_factory = get_session_factory(sqlite_database_url)
    with session_factory() as session:
        yield session


@pytest.fixture()
def client(sqlite_database_url) -> TestClient:
    app = create_app(get_settings())
    with TestClient(app) as test_client:
        yield test_client
