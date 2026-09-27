"""News provider transports. Push first; polling only where no push exists.

Providers normalize raw payloads into NormalizedNewsItem immediately on receipt and
hand them to `submit`; everything else happens in the pipeline. A provider failure
only affects its own health status, never the scanner.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.news.domain import NormalizedNewsItem, Transport
from app.news.metrics import ProviderHealth

logger = logging.getLogger(__name__)
Submit = Callable[[NormalizedNewsItem], Awaitable[bool]]


def payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


class SeenIds:
    """Bounded LRU of provider item ids (resubscription/poll overlap duplicates)."""

    def __init__(self, size: int = 5000) -> None:
        self.size, self.ids = size, OrderedDict[str, None]()

    def add(self, item_id: str) -> bool:
        """True if new."""
        if item_id in self.ids:
            self.ids.move_to_end(item_id)
            return False
        self.ids[item_id] = None
        if len(self.ids) > self.size:
            self.ids.popitem(last=False)
        return True


class NewsProvider:
    name: str
    transport: Transport

    def __init__(self, stale_after_seconds: float, interval: float | None) -> None:
        self.health = ProviderHealth(
            name=self.name,
            transport=self.transport,
            interval_seconds=interval,
            stale_after_seconds=stale_after_seconds,
        )
        self.seen = SeenIds()

    async def run(self, submit: Submit, stop: asyncio.Event) -> None:
        raise NotImplementedError

    async def _emit(self, submit: Submit, items: list[NormalizedNewsItem]) -> None:
        # Oldest first so causal order is preserved within a batch.
        for item in sorted(items, key=lambda i: i.published_at):
            if self.seen.add(item.id):
                await submit(item)


class PollingProvider(NewsProvider):
    """Fixed-rate polling (no drift) with exponential backoff on failure."""

    def __init__(self, interval: float, max_backoff: float = 300) -> None:
        super().__init__(stale_after_seconds=max(3 * interval, 120), interval=interval)
        self.interval, self.max_backoff = interval, max_backoff

    async def poll(self) -> list[NormalizedNewsItem]:
        raise NotImplementedError

    async def poll_batches(self) -> AsyncIterator[list[NormalizedNewsItem]]:
        """Override to hand each response to the pipeline as soon as it arrives
        (e.g. one request per category) instead of after the whole poll."""
        yield await self.poll()

    async def run(self, submit: Submit, stop: asyncio.Event) -> None:
        failures = 0
        next_at = time.monotonic()
        while not stop.is_set():
            self.health.attempt()
            try:
                async for batch in self.poll_batches():
                    await self._emit(submit, batch)
                self.health.success()
                failures = 0
                next_at += self.interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider outage stays isolated
                failures += 1
                self.health.errors += 1
                logger.warning(
                    "News provider %s failed error=%s", self.name, type(exc).__name__
                )
                backoff = min(self.max_backoff, self.interval * 2 ** min(failures, 6))
                next_at = time.monotonic() + backoff * random.uniform(0.8, 1.2)
            delay = max(0.0, next_at - time.monotonic())
            if delay == 0:
                next_at = time.monotonic()  # never burst to "catch up" missed polls
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass


class StreamingProvider(NewsProvider):
    """Persistent push connection: reconnect with backoff, heartbeat and stale watchdog.

    Subclasses implement `connect()` (async iterator of raw messages, raising on
    disconnect), `parse(message)` and optionally `heartbeat()`.
    """

    def __init__(
        self,
        stale_after_seconds: float = 90,
        heartbeat_seconds: float = 30,
        max_backoff: float = 60,
    ) -> None:
        super().__init__(stale_after_seconds=stale_after_seconds, interval=None)
        self.heartbeat_seconds, self.max_backoff = heartbeat_seconds, max_backoff
        self.last_message_mono: float | None = None

    def connect(self) -> AsyncIterator[Any]:
        raise NotImplementedError

    def parse(self, message: Any, received_at: datetime) -> list[NormalizedNewsItem]:
        raise NotImplementedError

    async def heartbeat(self) -> None:
        return None

    async def _consume(self, submit: Submit) -> None:
        async for message in self.connect():
            self.last_message_mono = time.monotonic()
            self.health.success()
            received_at = datetime.now(UTC)  # stamp before any parsing work
            await self._emit(submit, self.parse(message, received_at))

    async def _watch(self, reader: asyncio.Task) -> None:
        while not reader.done():
            await asyncio.sleep(
                min(self.heartbeat_seconds, self.health.stale_after_seconds / 3)
            )
            await self.heartbeat()
            silent = (
                time.monotonic() - self.last_message_mono
                if self.last_message_mono is not None
                else 0.0
            )
            if silent > self.health.stale_after_seconds:
                self.health.stale_events += 1
                logger.warning(
                    "News stream %s stale for %.0fs; reconnecting", self.name, silent
                )
                reader.cancel()
                return

    async def run(self, submit: Submit, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            session_started = self.last_message_mono = time.monotonic()
            reader = asyncio.create_task(self._consume(submit))
            watchdog = asyncio.create_task(self._watch(reader))
            stopper = asyncio.create_task(stop.wait())
            await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for task in (watchdog, stopper):
                task.cancel()
            if stop.is_set():
                reader.cancel()
                await asyncio.gather(reader, watchdog, stopper, return_exceptions=True)
                return
            outcome = await asyncio.gather(reader, return_exceptions=True)
            await asyncio.gather(watchdog, stopper, return_exceptions=True)
            error = outcome[0]
            if isinstance(error, BaseException) and not isinstance(
                error, asyncio.CancelledError
            ):
                self.health.errors += 1
                logger.warning(
                    "News stream %s dropped error=%s", self.name, type(error).__name__
                )
            self.health.reconnects += 1
            # Only a session that stayed up for a minute resets the backoff; a
            # flapping connection backs off exponentially instead of hammering.
            attempt = 1 if time.monotonic() - session_started >= 60 else attempt + 1
            delay = min(self.max_backoff, 2 ** min(attempt, 6)) * random.uniform(
                0.5, 1.0
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
