"""Shared database query helpers used across commands."""

from __future__ import annotations

import sqlite3


def fts_quote(text: str) -> str:
    """Turn a search string into a safe FTS5 OR query."""
    tokens = text.split()
    return " OR ".join(f'"{t}"' for t in tokens if t)


def find_memory(conn: sqlite3.Connection, name: str, include_archived: bool = False):
    """Find a memory by exact name → FTS → LIKE. Returns Row or None."""
    archived_clause = "" if include_archived else "AND archived_ts IS NULL"

    # Exact match.
    row = conn.execute(
        f"SELECT id, name, surface_count, useful_count, archived_ts "
        f"FROM memories WHERE name = ? {archived_clause}",
        (name,),
    ).fetchone()
    if row:
        return row

    # FTS match (only for non-archived by default).
    if not include_archived:
        fts = fts_quote(name)
        if fts:
            rows = conn.execute(
                "SELECT m.id, m.name, m.surface_count, m.useful_count, m.archived_ts "
                "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                f"WHERE memories_fts MATCH ? AND m.archived_ts IS NULL "
                "ORDER BY rank LIMIT 1",
                (fts,),
            ).fetchall()
            if rows:
                return rows[0]

    # LIKE fallback.
    row = conn.execute(
        f"SELECT id, name, surface_count, useful_count, archived_ts "
        f"FROM memories WHERE name LIKE ? {archived_clause} LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    return row

