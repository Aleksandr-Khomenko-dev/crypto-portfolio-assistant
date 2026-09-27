"""One short-lived mark-price-only subscription batch for OI unit conversion.

No funding/candle stream is subscribed. Shares the REST provider's request budget
(conservatively charging the handshake and each subscription against that budget).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import ssl
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import aiohttp
import certifi

from app.providers.request_control import RequestBudget


def parse_mark(
    message: str, boundary: datetime, now: datetime
) -> tuple[str, Decimal] | None:
    """Ignore acknowledgements, unrelated streams, malformed/stale/future prices."""
    try:
        payload = json.loads(message)
        if not isinstance(payload, dict) or not str(
            payload.get("dataType", "")
        ).endswith("@markPrice"):
            return None
        row = payload["data"]
        symbol = row["s"]
        if payload["dataType"] != symbol + "@markPrice":
            return None
        price = Decimal(str(row["p"]))
        # Live frame (verified 2026-09-24): {"code":0,"dataType":"BTC-USDT@markPrice",
        # "data":{"e":"markPriceUpdate","E":<event ms>,"s":"BTC-USDT","p":"83177.8"}}
        observed = datetime.fromtimestamp(int(row["E"]) / 1000, UTC)
        if price.is_finite() and price > 0 and boundary <= observed <= now:
            return symbol, price
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        pass
    return None


async def collect_mark_prices(
    url: str,
    symbols: list[str],
    boundary: datetime,
    budget: RequestBudget,
    timeout_seconds: float,
) -> dict[str, Decimal]:
    """One connection; a missing price fails only its symbol's OI observation."""
    wanted = set(symbols)
    marks: dict[str, Decimal] = {}
    if not wanted:
        return marks
    await budget.acquire(1)
    # Verify TLS against certifi's CA bundle, as httpx does for the REST calls. The
    # interpreter's default store can be empty (python.org macOS builds), which made
    # every connection fail certificate verification. Verification is never disabled.
    tls = ssl.create_default_context(cafile=certifi.where())
    async with (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            connector=aiohttp.TCPConnector(ssl=tls),
        ) as client,
        client.ws_connect(url, max_msg_size=1_048_576) as websocket,
    ):

        async def subscribe() -> None:
            for symbol in symbols:
                await budget.acquire(1)
                await websocket.send_json(
                    {"id": symbol, "reqType": "sub", "dataType": symbol + "@markPrice"}
                )

        async def receive() -> None:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.BINARY:
                    try:
                        content = gzip.decompress(message.data).decode()
                    except (OSError, UnicodeError):
                        continue
                elif message.type == aiohttp.WSMsgType.TEXT:
                    content = message.data
                else:
                    break
                if content == "Ping":
                    await websocket.send_str("Pong")
                    continue
                parsed = parse_mark(content, boundary, datetime.now(UTC))
                if parsed is not None and parsed[0] in wanted:
                    marks[parsed[0]] = parsed[1]
                if marks.keys() >= wanted:
                    return

        subscriber = asyncio.create_task(subscribe())
        receiver = asyncio.create_task(receive())
        try:
            await subscriber
            # Bound missing subscriptions separately from paced subscription sends.
            async with asyncio.timeout(timeout_seconds):
                await receiver
        except TimeoutError:
            pass  # keep the actual prices received; never substitute another price
        finally:
            subscriber.cancel()
            receiver.cancel()
            await asyncio.gather(subscriber, receiver, return_exceptions=True)
    return marks
