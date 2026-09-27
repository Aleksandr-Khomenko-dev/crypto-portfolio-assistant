"""Clock-aligned OI sampling, independent of technical analysis and its scan lock."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy.orm import Session, sessionmaker

from app.analytics.open_interest import OIObservation, closed_boundary, coverage
from app.scanner.domain import Contract, Ticker
from app.scanner.open_interest_repository import load_history, prune, record
from app.scanner.universe import filter_universe

if TYPE_CHECKING:
    from app.services.scanner_service import ScannerRuntime

logger = logging.getLogger(__name__)


class OISamplingProvider(Protocol):
    async def contracts(self) -> list[Contract]: ...
    async def tickers(self) -> dict[str, Ticker]: ...
    def oi_collection_priority(self) -> AbstractAsyncContextManager: ...
    async def oi_mark_prices(self, boundary: datetime) -> dict[str, Decimal]: ...
    async def oi_snapshot(
        self, symbol: str, boundary: datetime, mark: Decimal
    ) -> OIObservation: ...


class OICollectorService:
    def __init__(
        self,
        runtime: ScannerRuntime,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.runtime, self.sessions, self.clock = runtime, sessions, clock
        self.settings = runtime.settings

    def _persist(
        self,
        boundary: datetime,
        universe: list[str],
        observations: dict[str, OIObservation],
        now: datetime,
    ) -> tuple[Counter, dict]:
        counts: Counter = Counter()
        # Own session on this thread; all HTTP has finished. Atomic upserts resolve
        # duplicate workers without SELECT-before-INSERT or a long SQLite write lock.
        with self.sessions() as session:
            with session.begin():
                for symbol, observation in observations.items():
                    counts[record(session, "BINGX", symbol, observation, now)] += 1
                counts["pruned"] = prune(
                    session,
                    "BINGX",
                    now - timedelta(days=self.settings.oi_snapshot_retention_days),
                )
            history = load_history(
                session, "BINGX", universe, boundary - timedelta(hours=4)
            )
            diagnostic = coverage(history, universe, boundary, now)
        return counts, diagnostic

    async def collect(self, boundary: datetime) -> dict:
        if boundary.tzinfo is None or closed_boundary(boundary) != boundary:
            raise ValueError("OI target must be an aware 15-minute boundary")
        if self.clock() < boundary:
            raise ValueError("OI collection cannot run before its target boundary")
        if self.runtime.closing or self.runtime.oi_lock.locked():
            return {"status": "SKIPPED", "reason": "collector busy or closing"}
        if self.runtime.provider.exchange != "BINGX":
            return {
                "status": "SKIPPED",
                "reason": "active scanner exchange is not BingX",
            }
        async with self.runtime.oi_lock:
            self.runtime.oi_task = asyncio.current_task()
            started = time.monotonic()
            report: dict[str, Any] = {
                "boundary": boundary.isoformat(),
                "exchange": "BINGX",
                "status": "COMPLETED",
                **dict.fromkeys(
                    (
                        "oi_collection_requested",
                        "oi_collection_successful",
                        "oi_collection_failed",
                        "oi_collection_late",
                        "oi_collection_duplicates",
                        "oi_snapshots_inserted",
                        "oi_snapshots_replaced",
                        "oi_observations_unaligned",
                    ),
                    0,
                ),
                "errors": {},
            }
            deadline = boundary + timedelta(
                seconds=self.settings.oi_snapshot_tolerance_seconds
            )
            provider = cast(OISamplingProvider, self.runtime.provider)
            universe: list[str] = []
            observations: dict[str, OIObservation] = {}
            try:
                # No catch-up: an expired target is never relabelled as the current bucket.
                if self.clock() > deadline:
                    report.update(
                        status="SKIPPED", reason="target tolerance window expired"
                    )
                    return report
                # This is the SAME provider/cache/semaphore/budget as the scanner.
                # Allow in-flight requests to finish, then prioritize this short burst.
                async with provider.oi_collection_priority():
                    async with asyncio.timeout(
                        max(0.001, (deadline - self.clock()).total_seconds())
                    ):
                        responses = await asyncio.gather(
                            provider.contracts(),
                            provider.tickers(),
                            return_exceptions=True,
                        )
                        contracts, tickers = responses
                        if isinstance(contracts, BaseException):
                            raise contracts
                        if isinstance(tickers, BaseException):
                            raise tickers
                        universe = filter_universe(contracts, tickers, self.settings)
                        report["oi_collection_requested"] = len(universe)
                        marks = await provider.oi_mark_prices(boundary)
                        semaphore = asyncio.Semaphore(
                            self.settings.oi_collection_concurrency
                        )

                        async def sample(symbol: str) -> None:
                            async with semaphore:
                                try:
                                    observation = await provider.oi_snapshot(
                                        symbol, boundary, marks[symbol]
                                    )
                                    if (
                                        observation.bucket_at != boundary
                                        or observation.offset
                                        > self.settings.oi_snapshot_tolerance_seconds
                                        # Exchange timestamps may be slightly AHEAD
                                        # of this host's clock (measured live: ~80
                                        # ms). Only that small skew is tolerated; the
                                        # boundary, tolerance and staleness rules
                                        # above are unchanged.
                                        or observation.observed_at
                                        > self.clock()
                                        + timedelta(
                                            seconds=self.settings.oi_clock_skew_tolerance_seconds
                                        )
                                    ):
                                        report["oi_observations_unaligned"] += 1
                                        if observation.observed_at > deadline:
                                            report["oi_collection_late"] += 1
                                        return
                                    observations[symbol] = observation
                                except Exception as exc:  # noqa: BLE001 - isolate individual public endpoints
                                    report["errors"][symbol] = type(exc).__name__

                        await asyncio.gather(*(sample(symbol) for symbol in universe))
            except TimeoutError:
                report["oi_collection_late"] += len(universe) - len(observations)
                report["errors"]["deadline"] = "TimeoutError"
            except asyncio.CancelledError:
                report["status"] = "CANCELLED"
                raise
            except Exception as exc:  # noqa: BLE001 - failed metadata must not kill future jobs
                report["errors"]["collection"] = type(exc).__name__
            finally:
                try:
                    if report["status"] not in {"CANCELLED", "SKIPPED"}:
                        # Await persistence even if shutdown arrives here: no abandoned worker
                        # thread still using the DB after runtime shutdown reports completion.
                        task = asyncio.create_task(
                            asyncio.to_thread(
                                self._persist,
                                boundary,
                                universe,
                                observations,
                                self.clock(),
                            )
                        )
                        try:
                            counts, diagnostic = await asyncio.shield(task)
                        except asyncio.CancelledError:
                            await task
                            report["status"] = "CANCELLED"
                            raise
                        report["oi_collection_successful"] = len(observations)
                        report["oi_collection_duplicates"] = (
                            counts["kept"] + counts["replaced"]
                        )
                        report["oi_snapshots_inserted"] = counts["inserted"]
                        report["oi_snapshots_replaced"] = counts["replaced"]
                        report["oi_snapshots_pruned"] = counts["pruned"]
                        report["oi_coverage"] = diagnostic
                except Exception as exc:  # noqa: BLE001 - surface persistence failures in telemetry
                    report["errors"]["persistence"] = type(exc).__name__
                finally:
                    report["oi_collection_failed"] = (
                        report["oi_collection_requested"]
                        - report["oi_collection_successful"]
                    )
                    if report["status"] == "COMPLETED" and (
                        report["errors"] or report["oi_collection_failed"]
                    ):
                        report["status"] = (
                            "PARTIAL"
                            if report["oi_collection_successful"]
                            else "FAILED"
                        )
                    report["oi_collection_elapsed_seconds"] = round(
                        time.monotonic() - started, 3
                    )
                    self.runtime.oi_last_collection = report
                    self.runtime.oi_task = None
                    logger.info("OI collection %s", report)
            return report
