from app.api.routers.admin import router as admin_router
from app.api.routers.analytics import router as analytics_router
from app.api.routers.assets import router as assets_router
from app.api.routers.chart import router as chart_router
from app.api.routers.dashboard import router as dashboard_router
from app.api.routers.monitoring import router as monitoring_router
from app.api.routers.portfolios import router as portfolios_router
from app.api.routers.signals import router as signals_router

__all__ = [
    "admin_router",
    "analytics_router",
    "assets_router",
    "chart_router",
    "dashboard_router",
    "monitoring_router",
    "portfolios_router",
    "signals_router",
]
