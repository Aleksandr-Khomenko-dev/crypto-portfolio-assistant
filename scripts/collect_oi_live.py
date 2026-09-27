"""Read-only BingX validation across real clock boundaries, using an isolated DB.

Run: .venv/bin/python -m scripts.collect_oi_live --boundaries 2 --output-dir /tmp/cpda-oi-live
Copies only existing recent OI observations from the configured DB. Never starts
scanner scoring, portfolio monitoring, Telegram, or any exchange write endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.analytics.open_interest import next_boundary
from app.config import Settings
from app.db.models import Base
from app.db.scanner_models import OpenInterestSnapshot
from app.db.session import get_engine, get_session_factory
from app.providers.bingx_futures import BingXFuturesProvider
from app.services.oi_collector_service import OICollectorService
from app.services.scanner_service import ScannerRuntime


def copy_observed_history(source: str, destination: str) -> int:
    """Copy real observations only; never synthesize or shift their timestamps."""
    with get_session_factory(source)() as session:
        rows = session.scalars(
            select(OpenInterestSnapshot).where(
                OpenInterestSnapshot.exchange == "BINGX",
                OpenInterestSnapshot.bucket_at
                >= datetime.now(UTC) - timedelta(hours=5),
            )
        ).all()
        values = [
            {
                column.name: getattr(row, column.name)
                for column in OpenInterestSnapshot.__table__.columns
            }
            for row in rows
        ]
    with get_session_factory(destination).begin() as session:
        if values:
            session.execute(OpenInterestSnapshot.__table__.insert(), values)
    return len(values)


async def main(boundaries: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    database = output_dir / "observations.sqlite"
    if database.exists():
        raise ValueError(
            "Use a fresh output directory; existing results are never overwritten"
        )
    settings = Settings(
        scanner_provider="bingx",
        scanner_send_telegram=False,
        telegram_polling_enabled=False,
    )
    destination = f"sqlite:///{database.resolve()}"
    Base.metadata.create_all(get_engine(destination))
    copied = copy_observed_history(settings.database_url, destination)
    runtime = ScannerRuntime(BingXFuturesProvider(settings), settings)
    service = OICollectorService(runtime, get_session_factory(destination))
    target = next_boundary(datetime.now(UTC))
    metadata = {
        "started_at": datetime.now(UTC).isoformat(),
        "copied_real_history_rows": copied,
        "boundaries": boundaries,
        "first_target": target.isoformat(),
        "tolerance_seconds": settings.oi_snapshot_tolerance_seconds,
        "requests_per_second": settings.scanner_requests_per_second,
        "concurrency": settings.oi_collection_concurrency,
        "source_database_modified": False,
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata), flush=True)
    try:
        with (output_dir / "results.jsonl").open("a") as results:
            for _ in range(boundaries):
                while (remaining := (target - datetime.now(UTC)).total_seconds()) > 0:
                    await asyncio.sleep(min(remaining, 30))
                report = await service.collect(target)
                line = json.dumps(report)
                results.write(line + "\n")
                results.flush()
                print(line, flush=True)
                target = next_boundary(datetime.now(UTC))
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundaries", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.boundaries < 2:
        parser.error("Live acceptance requires at least two boundaries")
    asyncio.run(main(args.boundaries, args.output_dir))
