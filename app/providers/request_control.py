"""Public HTTP request pacing; all waiters recheck budgets after waking."""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable


class RateLimitBlockedError(RuntimeError):
    """The exchange imposed a cooldown longer than a scan cycle should wait."""


class RequestBudget:
    def __init__(
        self,
        rate: float,
        weight_per_minute: int,
        *,
        max_block_seconds: float = 120,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate, self.limit = rate, weight_per_minute
        self.clock, self.sleep = clock, sleep
        self.max_block_seconds = max_block_seconds
        # Sliding budget window: Binance weights per 60 s, BingX requests per 10 s.
        self.window = window_seconds
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.weights: deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()

    def defer(self, seconds: float) -> None:
        if not math.isfinite(seconds):
            seconds = 60
        self.blocked_until = max(self.blocked_until, self.clock() + max(1, seconds))

    async def acquire(self, weight: int) -> None:
        if weight > self.limit:
            raise ValueError("Request weight exceeds configured budget")
        while True:
            async with self.lock:
                now = self.clock()
                if self.blocked_until - now > self.max_block_seconds:
                    # A long 418 ban must fail the cycle, not hold the scan lock for hours.
                    raise RateLimitBlockedError("Exchange rate-limit cooldown active")
                while self.weights and self.weights[0][0] <= now - self.window:
                    self.weights.popleft()
                wait = max(self.next_request, self.blocked_until) - now
                if sum(w for _, w in self.weights) + weight > self.limit:
                    wait = max(wait, self.weights[0][0] + self.window - now)
                if wait <= 0:
                    self.next_request = now + 1 / self.rate
                    self.weights.append((now, weight))
                    return
            # Never reserve a slot before sleeping: another response may impose a ban.
            await self.sleep(wait)
