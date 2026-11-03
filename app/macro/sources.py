"""Free official macro data for gold (verified live 2026-11-03).

* US 10Y yield: U.S. Treasury daily par yield curve XML (``BC_10YEAR``), published
  after the New York close, so it is DAILY resolution (yesterday's close intraday).
* VIX: Cboe delayed quote JSON (about 15 minutes late) and the Cboe daily history CSV.
* DXY comes from the scanning exchange (BingX ``NCSIDXY2USD-USDT``), not from here.

Yahoo Finance answered HTTP 429 and FRED timed out from this host, so neither is used.
Both Cboe URLs answer with a Cloudflare 307 first, so redirects must be followed.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

import httpx

TREASURY_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
CBOE_HISTORY_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
)
USER_AGENT = "Mozilla/5.0 (compatible; CPDA macro context; read-only)"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_treasury_10y(xml_text: str | bytes) -> list[tuple[date, float]]:
    """(date, 10Y par yield %) oldest first; rows without a 10Y value are skipped."""
    root = ET.fromstring(xml_text)
    rows: dict[date, float] = {}
    for node in root.iter():
        if _local(node.tag) != "properties":
            continue
        values = {_local(child.tag): (child.text or "").strip() for child in node}
        raw_date, raw_yield = values.get("NEW_DATE"), values.get("BC_10YEAR")
        if not raw_date or not raw_yield:
            continue
        rows[datetime.fromisoformat(raw_date).date()] = float(raw_yield)
    return sorted(rows.items())


def parse_vix_history(csv_text: str, keep: int = 60) -> list[tuple[date, float]]:
    """Last `keep` daily VIX closes, oldest first (Cboe DATE is MM/DD/YYYY)."""
    rows = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            day = datetime.strptime(row["DATE"], "%m/%d/%Y").replace(tzinfo=UTC).date()
            rows.append((day, float(row["CLOSE"])))
        except (KeyError, ValueError):
            continue
    return sorted(rows)[-keep:]


def parse_vix_quote(payload: dict) -> float:
    return float(payload["data"]["current_price"])


async def fetch_us10y(
    client: httpx.AsyncClient, today: date
) -> list[tuple[date, float]]:
    """This year's daily 10Y yields; early in January the previous year is added."""
    years = [today.year] if today.month > 2 else [today.year - 1, today.year]
    rows: list[tuple[date, float]] = []
    for year in years:
        response = await client.get(
            TREASURY_URL,
            params={
                "data": "daily_treasury_yield_curve",
                "field_tdr_date_value": str(year),
            },
        )
        response.raise_for_status()
        rows += parse_treasury_10y(response.content)
    return sorted(dict(rows).items())


async def fetch_vix(
    client: httpx.AsyncClient,
) -> tuple[float, list[tuple[date, float]]]:
    quote = await client.get(CBOE_QUOTE_URL)
    quote.raise_for_status()
    history = await client.get(CBOE_HISTORY_URL)
    history.raise_for_status()
    return parse_vix_quote(quote.json()), parse_vix_history(history.text)


def new_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
