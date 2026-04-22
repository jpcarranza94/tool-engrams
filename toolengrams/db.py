"""SQLite connection + migration runner for ToolEngrams."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 6
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

DEFAULT_DB_PATH = Path.home() / ".claude" / "tool-engrams" / "db.sqlite"


def db_path() -> Path:
    """Resolve the DB path. Honors $ENGRAM_DB for tests and overrides."""
    override = os.environ.get("ENGRAM_DB")
    if override:
        return Path(override)
    return DEFAULT_DB_PATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection, creating the parent dir and running migrations as needed."""
    target = path or db_path()
    if target != Path(":memory:"):
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = _discover_latest_version()
    if current >= latest:
        return
    if current == 0:
        conn.executescript(SCHEMA_PATH.read_text())
        # Fresh DB — also apply all migrations so tables are complete.
        _apply_migrations(conn, from_version=2, to_version=latest)
        conn.execute(f"PRAGMA user_version = {latest}")
        return
    # Incremental migrations for existing DBs.
    _apply_migrations(conn, from_version=current + 1, to_version=latest)
    conn.execute(f"PRAGMA user_version = {latest}")


def _discover_latest_version() -> int:
    """Scan migrations/ for v*.sql files and return the highest version found.

    Falls back to SCHEMA_VERSION if no migration files exist (fresh install
    with only schema.sql).
    """
    versions = [SCHEMA_VERSION]
    if MIGRATIONS_DIR.is_dir():
        for path in MIGRATIONS_DIR.glob("v*.sql"):
            stem = path.stem  # e.g. "v2"
            try:
                versions.append(int(stem[1:]))
            except ValueError:
                continue
    return max(versions)


def _apply_migrations(
    conn: sqlite3.Connection,
    from_version: int,
    to_version: int,
) -> None:
    """Apply all migration files from from_version to to_version inclusive."""
    for v in range(from_version, to_version + 1):
        path = MIGRATIONS_DIR / f"v{v}.sql"
        if path.exists():
            conn.executescript(path.read_text())


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a BEGIN/COMMIT around the caller's writes."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
