"""SQLite connection + migration runner for ToolEngrams."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import paths

SCHEMA_VERSION = 18
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def db_path() -> Path:
    """Resolve the DB path. Honors $ENGRAM_DB for tests and overrides."""
    override = os.environ.get("ENGRAM_DB")
    if override:
        return Path(override)
    return paths.engram_home() / "db.sqlite"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection, creating the parent dir and running migrations as needed."""
    target = path or db_path()
    if target != Path(":memory:"):
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    # Wait on a contended write rather than failing fast with "database is
    # locked" — matters now that consolidation can run concurrently with the
    # watcher (and, briefly, with an overlapping sweep before the flock catches).
    conn.execute("PRAGMA busy_timeout = 5000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = _discover_latest_version()
    if current >= latest:
        return
    if current == 0:
        # Fresh DB: schema.sql is a complete v_latest snapshot — no migrations.
        conn.executescript(SCHEMA_PATH.read_text())
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
    """Apply all migration files from from_version to to_version inclusive.

    A missing in-range file is a hard error, NOT a skip: the migration chain is
    contiguous, so an absent `vN.sql` means the schema would fall behind the
    user_version that _migrate is about to stamp. Skipping then stamping is how a
    DB once ended up marked v17 with no `access_mode` column (the column-add
    migration was never run) — refuse loudly instead of corrupting the
    version<->schema invariant.
    """
    for v in range(from_version, to_version + 1):
        path = MIGRATIONS_DIR / f"v{v}.sql"
        if not path.exists():
            raise FileNotFoundError(
                f"migration {path.name} is missing but version {v} is in the "
                f"pending range [{from_version}, {to_version}]; refusing to stamp "
                f"user_version past a migration that never ran"
            )
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


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection, yield it, close on exit. Equivalent to:

        conn = db.connect()
        try:
            ...
        finally:
            conn.close()

    Use this at every call site that opens-then-closes a connection. Reserve
    raw `db.connect()` for the rare place that needs to hold a connection across
    an unusual lifecycle (e.g. a long-lived migration runner).
    """
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
