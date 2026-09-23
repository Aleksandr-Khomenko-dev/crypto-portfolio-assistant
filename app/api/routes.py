from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.deps import close_cached_provider, close_scanner_runtime
from app.api.routers import (
    admin_router,
    analytics_router,
    assets_router,
    chart_router,
    dashboard_router,
    monitoring_router,
    portfolios_router,
    signals_router,
)
from app.config import Settings, get_settings
from app.db.bootstrap import ensure_reference_data
from app.scheduler.jobs import build_scheduler

_STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"

router = APIRouter()


@router.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")


@router.get("/health", tags=["health"])
def healthcheck(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "environment": settings.environment,
        "provider_order": settings.provider_order,
        "scheduler_enabled": settings.scheduler_enabled,
        "telegram_enabled": bool(settings.telegram_bot_token),
        "read_only": True,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio
        from app.db.session import get_db_session
        from app.telegram.bot import run_bot

        with get_db_session() as session:
            ensure_reference_data(session)

        scheduler = None
        if settings.scheduler_enabled:
            scheduler = build_scheduler(settings)
            scheduler.start()

        bot_task = None
        if settings.telegram_polling_enabled and settings.telegram_bot_token:
            bot_task = asyncio.create_task(run_bot())

        try:
            yield
        finally:
            if bot_task is not None:
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            await close_cached_provider()
            await close_scanner_runtime()

    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    prefix = settings.api_prefix
    app.add_api_route("/", dashboard, include_in_schema=False)
    app.add_api_route("/dashboard", dashboard, include_in_schema=False)
    app.include_router(router, prefix=prefix)
    app.include_router(dashboard_router, prefix=prefix)
    app.include_router(portfolios_router, prefix=prefix)
    app.include_router(signals_router, prefix=prefix)
    app.include_router(monitoring_router, prefix=prefix)
    app.include_router(assets_router, prefix=prefix)
    app.include_router(analytics_router, prefix=prefix)
    app.include_router(chart_router, prefix=prefix)
    app.include_router(admin_router, prefix=prefix)
    from app.api.routers.scanner import router as scanner_router
    from app.api.routers.tradingview import router as tradingview_router

    app.include_router(scanner_router, prefix=prefix)
    app.include_router(tradingview_router, prefix=prefix)
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
