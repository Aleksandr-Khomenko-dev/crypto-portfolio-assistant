"""Upgrade/downgrade on disposable databases must preserve portfolio records."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from uuid import uuid4


def test_migration_chain_and_preserved_portfolio_data(tmp_path):
    path = tmp_path / "migration.db"
    env = {**os.environ, "CPDA_DATABASE_URL": f"sqlite:///{path}"}

    def migrate(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "20260406_0003_price_alerts")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO assets (id,symbol,is_active,metadata_json,created_at,updated_at) VALUES (?, 'KEEP', 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (uuid4().hex,),
        )
        connection.commit()
    migrate("upgrade", "head")
    migrate("check")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT symbol FROM assets").fetchall() == [("KEEP",)]
        assert (
            connection.execute("SELECT count(*) FROM scanner_runs").fetchone()[0] == 0
        )
        assert connection.execute("SELECT count(*) FROM setup_outcomes").fetchone()
        columns = [r[1] for r in connection.execute("PRAGMA table_info(scanner_runs)")]
        assert "telemetry" in columns
        setup_columns = [
            r[1] for r in connection.execute("PRAGMA table_info(market_setups)")
        ]
        assert "exchange" in setup_columns
        assert connection.execute(
            "SELECT count(*) FROM open_interest_snapshots"
        ).fetchone() == (0,)
    migrate("downgrade", "20260924_0007_oi_snapshots")
    with closing(sqlite3.connect(path)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert not {t for t in tables if t.startswith("news_")}
    migrate("downgrade", "20260923_0006_exchange")
    with closing(sqlite3.connect(path)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "open_interest_snapshots" not in tables
    migrate("downgrade", "-1")
    with closing(sqlite3.connect(path)) as connection:
        setup_columns = [
            r[1] for r in connection.execute("PRAGMA table_info(market_setups)")
        ]
        assert "exchange" not in setup_columns
    migrate("downgrade", "20260923_0004_scanner")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT symbol FROM assets").fetchall() == [("KEEP",)]
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "setup_outcomes" not in tables and "market_setups" in tables
    migrate("upgrade", "head")


def test_unversioned_upgrade_requires_validation_and_makes_backup(tmp_path):
    path = tmp_path / "legacy.db"
    env = {**os.environ, "CPDA_DATABASE_URL": f"sqlite:///{path}"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260406_0003_price_alerts"],
        env=env,
        capture_output=True,
        check=True,
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DELETE FROM alembic_version")
        connection.commit()
    denied = subprocess.run(
        [sys.executable, "-m", "scripts.upgrade_database"], env=env, capture_output=True
    )
    assert denied.returncode != 0
    allowed = subprocess.run(
        [sys.executable, "-m", "scripts.upgrade_database", "--adopt-existing"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert list(tmp_path.glob("legacy.db.backup-*"))
    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "20260924_0010_early_events"
        )
