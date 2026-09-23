from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.services.monitoring_service import MonitoringService

logger = logging.getLogger(__name__)


async def run_monitoring_job(run_reason: str = "scheduled-monitor") -> None:
    from app.api.deps import get_cached_provider

    settings = get_settings()
    with get_db_session() as session:
        provider = get_cached_provider()
        result = await MonitoringService(
            session, provider, settings=settings
        ).evaluate_all_portfolios(run_reason=run_reason)
        logger.info("Monitoring run completed for %s portfolios", len(result.results))


async def run_cleanup_job() -> None:
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.price_snapshot_retention_days
    )
    with get_db_session() as session:
        from sqlalchemy import delete
        from app.db.models import PriceSnapshot

        result = session.execute(
            delete(PriceSnapshot).where(PriceSnapshot.captured_at < cutoff)
        )
        session.commit()
        logger.info(
            "Cleaned up %s old price snapshots (older than %s days)",
            result.rowcount,
            settings.price_snapshot_retention_days,
        )


async def run_morning_digest_job() -> None:
    from app.api.deps import get_cached_provider

    settings = get_settings()
    with get_db_session() as session:
        provider = get_cached_provider()
        digests = await MonitoringService(
            session, provider, settings=settings
        ).create_all_morning_digests()
        logger.info("Morning digests generated: %s", len(digests))


def build_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tzinfo)
    scheduler.add_job(
        run_monitoring_job,
        trigger="interval",
        minutes=settings.monitoring_interval_minutes,
        kwargs={"run_reason": "scheduled-monitor"},
        id="portfolio-monitor",
        replace_existing=True,
    )
    scheduler.add_job(
        run_morning_digest_job,
        trigger=CronTrigger(
            hour=settings.morning_digest_hour,
            minute=settings.morning_digest_minute,
            timezone=settings.tzinfo,
        ),
        id="morning-digest",
        replace_existing=True,
    )
    scheduler.add_job(
        run_cleanup_job,
        trigger=CronTrigger(hour=3, minute=0, timezone=settings.tzinfo),
        id="price-snapshot-cleanup",
        replace_existing=True,
    )
    if settings.scanner_enabled:
        scheduler.add_job(
            run_scanner_job,
            trigger="interval",
            minutes=settings.scanner_interval_minutes,
            id="market-scanner",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler


async def run_scanner_job() -> None:
    from app.api.deps import get_scanner_runtime
    from app.services.scanner_service import ScannerBusyError, ScannerService

    try:
        with get_db_session() as session:
            await ScannerService(session, get_scanner_runtime()).run()
    except ScannerBusyError:
        logger.info("Scanner job skipped: scan already in progress")
    except Exception as exc:
        logger.error("Scanner job failed error=%s", type(exc).__name__)
