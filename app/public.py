"""Internet-facing app: ONLY a minimal health check and the TradingView webhooks.

Run it next to (not instead of) the internal API:

    uvicorn app.public:app --host 127.0.0.1 --port 8001 --no-access-log

The internal API (`app.main`: portfolio, dashboard, admin, scanner, research) is never
mounted here and stays on localhost. This app also never starts Telegram polling or
the scheduler: the internal API process owns those (one poller per bot token).
Run uvicorn with --no-access-log: webhook URLs carry a secret token in the path.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import Settings, get_settings


def create_public_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    from app.api.routers.tradingview import router as tradingview_router

    # No docs/OpenAPI schema on the public side: nothing to enumerate.
    app = FastAPI(
        title=f"{settings.app_name} webhooks",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}  # no configuration details on the public side

    from app.tv.webhook import router as ict_router

    # ICT v4.2 (token in the path): stored raw and answered at once; the runner's
    # worker does the rest. Legacy QS PRO Gold (secret in the body) keeps its path.
    app.include_router(ict_router)
    app.include_router(tradingview_router, prefix=settings.api_prefix)
    return app


app = create_public_app()
