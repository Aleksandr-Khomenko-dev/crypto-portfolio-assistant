"""Economic calendar: high-impact events -> setup blackout and Telegram reminders.

Source: ForexFactory weekly JSON (verified 2026-11-03: title, country, ISO date with
New York offset, impact, forecast, previous). It covers the CURRENT week only; the
next week appears when the week rolls over. `calendar_extra_events` adds events by
hand (e.g. an unscheduled Fed press conference).

Scheduled data moves gold within milliseconds of the release, faster than any feed a
retail bot can read, so the edge is not speed but timing: no new setups shortly before
and after the release, and a reminder in advance. Holidays are not modelled.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.config import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

CACHE_PATH = PROJECT_ROOT / ".cache" / "economic_calendar.json"
IMPACTS = {"High": {"High"}, "Medium": {"High", "Medium"}}


@dataclass(frozen=True)
class EconomicEvent:
    title: str
    at: datetime  # timezone-aware
    currency: str = "USD"
    impact: str = "High"
    forecast: str = ""
    previous: str = ""
    source: str = "ForexFactory"

    @property
    def slot(self) -> str:
        """Events released together (NFP + unemployment) share one reminder."""
        return f"{self.currency}:{self.at.astimezone(UTC).isoformat()}"


def parse_forexfactory(
    rows: list[dict], currencies: set[str], min_impact: str
) -> list[EconomicEvent]:
    allowed = IMPACTS[min_impact]
    events = []
    for row in rows:
        if row.get("country") not in currencies or row.get("impact") not in allowed:
            continue
        try:
            at = datetime.fromisoformat(str(row["date"]))
        except (KeyError, ValueError):
            continue
        if at.tzinfo is None:
            continue
        events.append(
            EconomicEvent(
                title=str(row.get("title") or "").strip() or "Event",
                at=at,
                currency=str(row["country"]),
                impact=str(row["impact"]),
                forecast=str(row.get("forecast") or ""),
                previous=str(row.get("previous") or ""),
            )
        )
    return events


def parse_extra(raw: str) -> list[EconomicEvent]:
    """ "2026-12-09T14:00-05:00 FOMC statement|..." (ISO time WITH offset, then title)."""
    events = []
    for chunk in raw.split("|"):
        stamp, _, title = chunk.strip().partition(" ")
        if not stamp:
            continue
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            logger.warning("Calendar extra event ignored: bad time %r", stamp)
            continue
        if at.tzinfo is None:
            logger.warning("Calendar extra event ignored: no UTC offset in %r", stamp)
            continue
        events.append(
            EconomicEvent(
                title=title.strip() or "Event", at=at, impact="Manual", source="manual"
            )
        )
    return events


class EconomicCalendar:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        cache_path: Path | None = CACHE_PATH,
    ) -> None:
        self.settings = settings
        self.client = client
        self.cache_path = cache_path
        self.feed: list[EconomicEvent] = []
        self.fetched_at: datetime | None = None
        self.reminded: set[str] = set()
        self.last_error: str | None = None
        self._load()

    # ---- data ----------------------------------------------------------------
    @property
    def events(self) -> list[EconomicEvent]:
        merged = {
            (e.slot, e.title): e
            for e in self.feed + parse_extra(self.settings.calendar_extra_events)
        }
        return sorted(merged.values(), key=lambda e: (e.at, e.title))

    def stale(self, now: datetime) -> bool:
        return self.fetched_at is None or now - self.fetched_at >= timedelta(
            minutes=self.settings.calendar_refresh_minutes
        )

    async def refresh(self, now: datetime | None = None, force: bool = False) -> bool:
        """Fetch when stale. A failed fetch keeps the previous events (never empties)."""
        now = now or datetime.now(UTC)
        if not force and not self.stale(now):
            return False
        client = self.client or httpx.AsyncClient(
            timeout=self.settings.http_timeout_seconds,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; CPDA calendar; read-only)"
            },
        )
        try:
            response = await client.get(self.settings.calendar_url)
            response.raise_for_status()
            currencies = {
                c.strip().upper()
                for c in self.settings.calendar_currencies.split(",")
                if c.strip()
            }
            self.feed = parse_forexfactory(
                response.json(), currencies, self.settings.calendar_min_impact
            )
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - calendar outage keeps last events
            self.last_error = type(exc).__name__
            logger.warning("Economic calendar refresh failed error=%s", self.last_error)
        finally:
            if self.client is None:
                await client.aclose()
        # A failure is retried after one refresh period too (fair use of the feed).
        self.fetched_at = now
        self._save()
        return self.last_error is None

    # ---- questions -------------------------------------------------------------
    def window(self, event: EconomicEvent) -> tuple[datetime, datetime]:
        return (
            event.at - timedelta(minutes=self.settings.calendar_pre_minutes),
            event.at + timedelta(minutes=self.settings.calendar_post_minutes),
        )

    def blackout(self, now: datetime) -> list[EconomicEvent]:
        """Events whose pause window covers `now` (several when released together)."""
        return [e for e in self.events if self.window(e)[0] <= now <= self.window(e)[1]]

    def upcoming(self, now: datetime, within: timedelta) -> list[EconomicEvent]:
        return [e for e in self.events if now <= e.at <= now + within]

    def due_reminders(self, now: datetime) -> list[list[EconomicEvent]]:
        """Groups of same-time events whose reminder time has come and not been sent.
        Missed reminders (process down) are not sent after the event has started."""
        lead = timedelta(minutes=self.settings.calendar_reminder_minutes)
        groups: dict[str, list[EconomicEvent]] = {}
        for event in self.events:
            if event.slot not in self.reminded and event.at - lead <= now < event.at:
                groups.setdefault(event.slot, []).append(event)
        return list(groups.values())

    def mark_reminded(self, group: list[EconomicEvent]) -> None:
        self.reminded.update(e.slot for e in group)
        cutoff = datetime.now(UTC) - timedelta(days=14)
        self.reminded = {
            slot
            for slot in self.reminded
            if datetime.fromisoformat(slot.split(":", 1)[1]) >= cutoff
        }
        self._save()

    # ---- persistence -----------------------------------------------------------
    def _save(self) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
                "events": [{**asdict(e), "at": e.at.isoformat()} for e in self.feed],
                "reminded": sorted(self.reminded),
            }
            self.cache_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning("Calendar cache not saved error=%s", type(exc).__name__)

    def _load(self) -> None:
        if self.cache_path is None or not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.feed = [
                EconomicEvent(**{**row, "at": datetime.fromisoformat(row["at"])})
                for row in data.get("events", [])
            ]
            self.reminded = set(data.get("reminded", []))
            # Re-fetch after a restart, but keep cached events until that succeeds.
            self.fetched_at = None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.warning("Calendar cache ignored error=%s", type(exc).__name__)
