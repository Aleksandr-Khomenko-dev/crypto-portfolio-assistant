"""POST /webhook/tv/{token}: store the raw request and answer 200 at once.

TradingView cancels a webhook after 3 seconds, so nothing slow happens here: the
request is checked (token, sender IP, size), written to `raw_webhook` and answered.
Parsing, filters, context and Telegram run in the runner's worker (app.tv.worker),
so a restart never loses an accepted request.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.db.scanner_models import RawWebhook
from app.db.session import get_session_factory
from app.tv.ict import decode

router = APIRouter()
logger = logging.getLogger(__name__)
Configuration = Annotated[Settings, Depends(get_settings)]


def allowed_ips(settings: Settings) -> set[str]:
    return {ip.strip() for ip in settings.tv_allowed_ips.split(",") if ip.strip()}


def redact_legacy(body: str, settings: Settings) -> tuple[bool | None, str]:
    """Legacy QS PRO Gold bodies carry a secret: verify it now, never store it."""
    data = decode(body)
    if data is None or "secret" not in data:
        return None, body
    given = str(data.get("secret") or "")
    expected = settings.tradingview_webhook_secret or ""
    ok = bool(expected) and secrets.compare_digest(given.encode(), expected.encode())
    data["secret"] = "***"
    return ok, json.dumps(data, ensure_ascii=False)


def _store(row: RawWebhook) -> int:
    with get_session_factory()() as session:
        session.add(row)
        session.commit()
        return row.id


@router.post("/webhook/tv/{token}", include_in_schema=False)
async def receive(token: str, request: Request, settings: Configuration) -> dict:
    expected = settings.tv_webhook_token
    # Unknown token (or the route switched off): indistinguishable from no route.
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(404, "Not Found")
    ip = request.client.host if request.client else None
    if settings.tv_ip_allowlist_enabled and ip not in allowed_ips(settings):
        logger.warning("TradingView webhook rejected: sender IP not allowlisted")
        raise HTTPException(403, "Forbidden")
    body = await request.body()
    if len(body) > settings.tv_max_body_bytes:
        raise HTTPException(413, "Payload too large")
    text = body.decode("utf-8", errors="replace")
    secret_ok, stored = redact_legacy(text, settings)
    row = RawWebhook(
        received_at=datetime.now(UTC),
        client_ip=ip,
        content_type=(request.headers.get("content-type") or "")[:100] or None,
        body=stored,
        secret_ok=secret_ok,
        status="new",
    )
    row_id = await run_in_threadpool(_store, row)
    logger.info("TradingView webhook stored id=%s bytes=%s", row_id, len(body))
    return {"status": "accepted"}
