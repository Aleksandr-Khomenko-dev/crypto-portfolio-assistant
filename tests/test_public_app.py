"""The internet-facing app exposes only the health check and the TradingView webhook."""

from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.public import create_public_app

INTERNAL_PATHS = [
    "/",
    "/dashboard",
    "/api/health",
    "/api/portfolios",
    "/api/dashboard/summary",
    "/api/scanner/status",
    "/api/admin/import",
    "/docs",
    "/redoc",
    "/openapi.json",
]


@pytest.fixture()
def public(sqlite_database_url, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the public app must not start polling or the scheduler")

    monkeypatch.setattr("app.telegram.bot.run_bot", forbidden)
    monkeypatch.setattr("app.scheduler.jobs.build_scheduler", forbidden)
    app = create_public_app(get_settings())
    with TestClient(app) as client:  # runs startup: must not touch bot/scheduler
        yield client


def test_only_health_and_webhooks_are_routed(public):
    paths = {r.path for r in public.app.routes if isinstance(r, APIRoute)}
    assert paths == {"/health", "/api/webhooks/tradingview"}


def test_health_reveals_no_configuration(public):
    response = public.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", INTERNAL_PATHS)
def test_internal_api_and_docs_are_not_reachable(public, path):
    assert public.get(path).status_code == 404


def test_legacy_webhook_works_through_the_public_app(public):
    payload = {
        "event_id": "public-1",
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "event": "BOS",
        "direction": "LONG",
        "price": "100",
        "timestamp": datetime.now(UTC).isoformat(),
        "secret": "x" * 24,
    }
    disabled = public.post("/api/webhooks/tradingview", json=payload)
    assert (
        disabled.status_code == 404 and disabled.json()["detail"] == "Webhook disabled"
    )
    settings = Settings(
        tradingview_webhook_enabled=True, tradingview_webhook_secret="x" * 24
    )
    public.app.dependency_overrides[get_settings] = lambda: settings
    try:
        wrong = public.post(
            "/api/webhooks/tradingview", json={**payload, "secret": "no"}
        )
        assert wrong.status_code == 401
        ok = public.post("/api/webhooks/tradingview", json=payload)
        assert ok.status_code == 200 and ok.json()["accepted"]
    finally:
        public.app.dependency_overrides.clear()
