"""Shared database query helpers used across commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path


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


def get_existing_memory_names(conn: sqlite3.Connection) -> list[str]:
    """Get names of all active memories."""
    rows = conn.execute(
        "SELECT name FROM memories WHERE archived_ts IS NULL ORDER BY id"
    ).fetchall()
    return [r["name"] for r in rows]


def get_existing_memories_summary(conn: sqlite3.Connection) -> str:
    """Compact summary of active memories for dedup context."""
    rows = conn.execute(
        "SELECT name, body FROM memories WHERE archived_ts IS NULL ORDER BY id"
    ).fetchall()
    if not rows:
        return "No existing memories."
    return "\n".join(f"- {r['name']}: {r['body'][:100]}" for r in rows)


def get_memory_summary_detailed(db_path: Path) -> str:
    """Detailed memory state for consolidation agent context."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db_path))
    conn.row_factory = _sqlite3.Row

    memories = conn.execute(
        "SELECT m.id, m.name, m.body, m.type, m.surface_count, m.useful_count "
        "FROM memories m WHERE m.archived_ts IS NULL ORDER BY m.id"
    ).fetchall()

    lines = [f"Active memories ({len(memories)}):"]
    for m in memories:
        usefulness = (m["useful_count"] + 1.0) / (m["surface_count"] + 2.0)
        lines.append(
            f"  [{m['id']}] \"{m['name']}\" type={m['type']} "
            f"surfaces={m['surface_count']} useful={m['useful_count']} "
            f"usefulness={usefulness:.2f}"
        )
        lines.append(f"       body: {m['body'][:150]}")

    surfaces = conn.execute(
        "SELECT ss.memory_id, m.name, ss.session_id, ss.hook "
        "FROM session_surfaces ss JOIN memories m ON m.id = ss.memory_id "
        "ORDER BY ss.surfaced_ts DESC LIMIT 20"
    ).fetchall()
    lines.append(f"\nRecent surfaces ({len(surfaces)}):")
    for s in surfaces:
        lines.append(
            f"  memory={s['memory_id']} \"{s['name']}\" "
            f"session={s['session_id'][:12]}... hook={s['hook']}"
        )

    conn.close()
    return "\n".join(lines)
