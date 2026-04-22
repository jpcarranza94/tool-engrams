"""Formation CLI: `engram recall` — browse and search the memory store.

`engram recall`          → list all non-archived memories
`engram recall <query>`  → FTS search, ranked by relevance
`engram recall --stats`  → summary counts by type/scope
`engram recall --id N`   → full detail for one memory
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import db
from ..queries import fts_quote


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    conn = db.connect()
    try:
        if args.stats:
            return _show_stats(conn)
        if args.id:
            return _show_detail(conn, args.id)
        if args.query:
            return _search(conn, args.query, args.limit)
        return _list_all(conn, args.limit)
    finally:
        conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram recall")
    parser.add_argument("query", nargs="?", default=None, help="FTS search query.")
    parser.add_argument("--limit", type=int, default=20, help="Max results (default 20).")
    parser.add_argument("--stats", action="store_true", help="Show summary counts.")
    parser.add_argument("--id", type=int, default=None, help="Show full detail for one memory.")
    return parser.parse_args(argv)


def _list_all(conn, limit: int) -> int:
    rows = conn.execute(
        "SELECT m.id, m.name, m.kind, m.scope, m.project_slug, "
        "m.surface_count, m.useful_count, m.pinned, m.created_ts, m.archived_ts "
        "FROM memories m WHERE m.archived_ts IS NULL "
        "ORDER BY m.created_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    print(json.dumps({"count": len(rows), "memories": [_row_summary(r) for r in rows]}))
    return 0


def _search(conn, query: str, limit: int) -> int:
    fts_query = fts_quote(query)
    if not fts_query:
        return _list_all(conn, limit)

    rows = conn.execute(
        "SELECT m.id, m.name, m.kind, m.scope, m.project_slug, "
        "m.surface_count, m.useful_count, m.pinned, m.created_ts, m.archived_ts "
        "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND m.archived_ts IS NULL "
        "ORDER BY rank LIMIT ?",
        (fts_query, limit),
    ).fetchall()
    print(json.dumps({"query": query, "count": len(rows), "memories": [_row_summary(r) for r in rows]}))
    return 0


def _show_detail(conn, memory_id: int) -> int:
    row = conn.execute(
        "SELECT id, name, description, body, kind, scope, project_slug, "
        "surface_count, useful_count, pinned, created_ts, last_surfaced_ts, archived_ts "
        "FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    if not row:
        print(json.dumps({"error": "not_found", "id": memory_id}))
        return 1

    triggers = conn.execute(
        "SELECT kind, first_token, tokens_json, path_pattern "
        "FROM triggers WHERE memory_id = ?",
        (memory_id,),
    ).fetchall()

    surfaces = conn.execute(
        "SELECT session_id, hook, surfaced_ts FROM session_surfaces "
        "WHERE memory_id = ? ORDER BY surfaced_ts DESC LIMIT 10",
        (memory_id,),
    ).fetchall()

    print(json.dumps({
        "memory": dict(row),
        "triggers": [dict(t) for t in triggers],
        "recent_surfaces": [dict(s) for s in surfaces],
    }))
    return 0


def _show_stats(conn) -> int:
    kind_counts = conn.execute(
        "SELECT kind, COUNT(*) as count FROM memories "
        "WHERE archived_ts IS NULL GROUP BY kind"
    ).fetchall()
    scope_counts = conn.execute(
        "SELECT scope, COUNT(*) as count FROM memories "
        "WHERE archived_ts IS NULL GROUP BY scope"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned, "
        "SUM(CASE WHEN archived_ts IS NOT NULL THEN 1 ELSE 0 END) as archived "
        "FROM memories"
    ).fetchone()
    trigger_counts = conn.execute(
        "SELECT triggers.kind AS kind, COUNT(*) as count FROM triggers "
        "JOIN memories m ON triggers.memory_id = m.id "
        "WHERE m.archived_ts IS NULL GROUP BY triggers.kind"
    ).fetchall()

    print(json.dumps({
        "total": total["total"],
        "active": total["total"] - (total["archived"] or 0),
        "pinned": total["pinned"] or 0,
        "archived": total["archived"] or 0,
        "by_kind": {r["kind"]: r["count"] for r in kind_counts},
        "by_scope": {r["scope"]: r["count"] for r in scope_counts},
        "triggers_by_kind": {r["kind"]: r["count"] for r in trigger_counts},
    }))
    return 0


def _row_summary(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "scope": row["scope"],
        "surface_count": row["surface_count"],
        "useful_count": row["useful_count"],
        "pinned": bool(row["pinned"]),
    }
