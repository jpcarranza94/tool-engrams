"""SQLite connection + migration runner for ToolEngrams."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

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
    if current >= SCHEMA_VERSION:
        return
    if current == 0:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    # Future migrations go here: if current < 2: ...


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
