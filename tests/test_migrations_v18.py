"""v18 migration adds the consolidation_recommendations table + index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _indexes(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}


def test_fresh_db_has_recommendations_table(tmp_path: Path):
    conn = db.connect(tmp_path / "fresh.sqlite")
    assert "consolidation_recommendations" in _tables(conn)
    assert "idx_consol_recs_run_date" in _indexes(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_v17_db_gains_recommendations_table(tmp_path: Path):
    """A simulated v17 DB (no recommendations table) upgrades in place: the new
    table appears, the user_version advances, and prior data is untouched."""
    path = tmp_path / "v17.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    raw.executescript("DROP TABLE consolidation_recommendations;")
    raw.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts) VALUES ('2026-06-01', 1)")
    raw.execute("PRAGMA user_version = 17")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 18
    assert "consolidation_recommendations" in _tables(conn)
    # Pre-existing run row survives the upgrade.
    assert conn.execute(
        "SELECT COUNT(*) FROM consolidation_runs").fetchone()[0] == 1
    conn.close()


def test_migration_is_rerunnable(tmp_path: Path):
    """Re-applying v18 (IF NOT EXISTS) over an already-migrated DB is a no-op."""
    path = tmp_path / "rerun.sqlite"
    conn = db.connect(path)
    conn.executescript((db.MIGRATIONS_DIR / "v18.sql").read_text())  # re-run by hand
    assert "consolidation_recommendations" in _tables(conn)
    conn.close()
