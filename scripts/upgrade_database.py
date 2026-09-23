"""Upgrade a configured DB; explicitly adopt validated, unversioned SQLite installs.

Usage: python -m scripts.upgrade_database [--adopt-existing]
SQLite backups are made before stamping or migration. Existing portfolio rows are never rewritten.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import models  # noqa: F401
from app.db.base import Base

BASELINE = "20260406_0003_price_alerts"
# Tables created by migrations after the baseline; absent from legacy installs.
SCANNER_TABLES = {
    "scanner_runs",
    "scanner_snapshots",
    "market_setups",
    "tradingview_events",
    "setup_outcomes",
}


def validate_existing(engine) -> None:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names()) - {"alembic_version"}
    expected = set(Base.metadata.tables) - SCANNER_TABLES
    if existing != expected:
        raise ValueError(
            f"Unversioned table set differs from baseline: {sorted(existing ^ expected)}"
        )
    for name in sorted(expected):
        table = Base.metadata.tables[name]
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        if set(columns) != set(table.columns.keys()):
            raise ValueError(f"{name}: columns differ from baseline")
        for column in table.columns:
            old = columns[column.name]
            if (
                str(old["type"]) != column.type.compile(dialect=engine.dialect)
                or old["nullable"] != column.nullable
                or bool(old["primary_key"]) != column.primary_key
            ):
                raise ValueError(
                    f"{name}.{column.name}: type, nullability or primary key differs"
                )
        old_foreign = {
            (
                tuple(f["constrained_columns"]),
                f["referred_table"],
                tuple(f["referred_columns"]),
            )
            for f in inspector.get_foreign_keys(name)
        }
        new_foreign = {
            (
                tuple(c.name for c in f.columns),
                f.referred_table.name,
                tuple(e.column.name for e in f.elements),
            )
            for f in table.foreign_key_constraints
        }
        if old_foreign != new_foreign:
            raise ValueError(f"{name}: foreign keys differ")
        # create_all historically represented symbol uniqueness as a unique index.
        old_unique = {
            tuple(c["column_names"]) for c in inspector.get_unique_constraints(name)
        }
        old_unique |= {
            tuple(i["column_names"]) for i in inspector.get_indexes(name) if i["unique"]
        }
        from sqlalchemy import UniqueConstraint

        new_unique = {
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        new_unique |= {
            tuple(c.name for c in index.columns)
            for index in table.indexes
            if index.unique
        }
        if old_unique != new_unique:
            raise ValueError(f"{name}: uniqueness differs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adopt-existing", action="store_true")
    args = parser.parse_args()
    url = get_settings().database_url
    engine = create_engine(url)
    with engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        version = (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if "alembic_version" in tables
            else None
        )
    populated = bool(set(tables) - {"alembic_version"})
    adopt = populated and not version
    if adopt:
        if not args.adopt_existing or engine.dialect.name != "sqlite":
            raise SystemExit(
                "Unversioned database: use --adopt-existing for a validated SQLite baseline, or migrate manually."
            )
        validate_existing(engine)
    if engine.dialect.name == "sqlite" and populated:
        source = Path(make_url(url).database).resolve()
        backup = source.with_name(
            source.name + ".backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        )
        with (
            closing(sqlite3.connect(source)) as src,
            closing(sqlite3.connect(backup)) as dst,
        ):
            src.backup(dst)
        print(f"Backup: {backup}")
    engine.dispose()
    env = {**os.environ, "CPDA_DATABASE_URL": url}
    if adopt:
        subprocess.run(
            [sys.executable, "-m", "alembic", "stamp", BASELINE], env=env, check=True
        )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True
    )


if __name__ == "__main__":
    main()
