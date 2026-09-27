"""BingX public swap WebSocket (verified live 2026-09-24).

wss://open-api-swap.bingx.com/swap-market. There is no all-market stream (`all@ticker`
is rejected, code 80015), so per-symbol `{SYMBOL}@kline_1m` subscriptions are
multiplexed over a few shared connections. Frames are gzip; the server sends "Ping"
and expects "Pong". The kline payload is {o,h,l,c,v,T} where T is the minute OPEN
time: it carries no event timestamp, so only our receipt time is measurable.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import random
import ssl
import time
from collections.abc import Callable

import aiohttp
import certifi

logger = logging.getLogger(__name__)
URL = "wss://open-api-swap.bingx.com/swap-market"
# (bingx symbol, open_ms, close, volume, received_epoch_seconds, high, low, open)
OnKline = Callable[..., None]


def parse_frame(raw: bytes | str) -> dict | None:
    text = gzip.decompress(raw).decode() if isinstance(raw, bytes) else raw
    if text == "Ping":
        return {"ping": True}
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class StreamConnection:
    """One multiplexed connection with its own subscriptions and reconnect loop."""

    def __init__(
        self,
        index: int,
        symbols: list[str],
        on_kline: OnKline,
        stale_after: float = 30,
        max_backoff: float = 30,
        connect: Callable | None = None,
    ) -> None:
        self.index, self.symbols, self.on_kline = index, symbols, on_kline
        self.stale_after, self.max_backoff = stale_after, max_backoff
        self._connect = connect  # injectable for tests
        self.reconnects = 0
        self.errors = 0
        self.last_message: float | None = None
        self.messages = 0

    async def _open(self, session: aiohttp.ClientSession):
        return await session.ws_connect(URL, heartbeat=None, max_msg_size=4_000_000)

    async def _session(self, session: aiohttp.ClientSession | None) -> None:
        if self._connect is not None:
            ws = await self._connect()
        else:
            assert session is not None
            ws = await self._open(session)
        try:
            for symbol in self.symbols:
                await ws.send_json(
                    {"id": symbol, "reqType": "sub", "dataType": f"{symbol}@kline_1m"}
                )
            self.last_message = time.monotonic()
            while True:
                message = await asyncio.wait_for(ws.receive(), timeout=self.stale_after)
                if message.type in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                    received = time.time()  # stamp before any parsing
                    self.last_message = time.monotonic()
                    payload = parse_frame(message.data)
                    if payload is None:
                        continue
                    if payload.get("ping"):
                        await ws.send_str("Pong")
                        continue
                    self.messages += 1
                    self._dispatch(payload, received)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise ConnectionError("stream closed")
        finally:
            await ws.close()

    def _dispatch(self, payload: dict, received: float) -> None:
        data_type = str(payload.get("dataType") or "")
        if not data_type.endswith("@kline_1m"):
            return
        for row in payload.get("data") or []:
            try:
                self.on_kline(
                    data_type.split("@")[0],
                    int(row["T"]),
                    float(row["c"]),
                    float(row["v"]),
                    received,
                    float(row.get("h", row["c"])),
                    float(row.get("l", row["c"])),
                    float(row.get("o", row["c"])),
                )
            except (KeyError, TypeError, ValueError):
                continue

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        tls = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=tls)
        ) as session:
            while not stop.is_set():
                started = time.monotonic()
                reader = asyncio.create_task(self._session(session))
                stopper = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait(
                        {reader, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError:
                    # Cancelled from outside (shutdown): never orphan the reader.
                    reader.cancel()
                    stopper.cancel()
                    await asyncio.gather(reader, stopper, return_exceptions=True)
                    raise
                stopper.cancel()
                if stop.is_set():
                    reader.cancel()
                    await asyncio.gather(reader, stopper, return_exceptions=True)
                    return
                error = (await asyncio.gather(reader, return_exceptions=True))[0]
                self.errors += 1
                self.reconnects += 1
                logger.warning(
                    "Fast stream %s reconnecting error=%s",
                    self.index,
                    type(error).__name__
                    if isinstance(error, BaseException)
                    else "closed",
                )
                attempt = 1 if time.monotonic() - started >= 60 else attempt + 1
                delay = min(self.max_backoff, 2 ** min(attempt, 5)) * random.uniform(
                    0.5, 1.0
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    def age_ms(self) -> float | None:
        if self.last_message is None:
            return None
        return round((time.monotonic() - self.last_message) * 1000, 1)
