"""Formation CLI: `engram recall` — browse and search the memory store.

`engram recall`          → list all non-archived memories
`engram recall <query>`  → FTS search, ranked by relevance
`engram recall --stats`  → summary counts by type/scope
`engram recall --id N`   → full detail for one memory
"""

from __future__ import annotations

import argparse
import dataclasses
import json

from .. import db, memory_store
from ..models import Memory, Trigger


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        if args.stats:
            return _show_stats(conn)
        if args.id:
            return _show_detail(conn, args.id)
        if args.query:
            return _search(conn, args.query, args.limit)
        return _list_all(conn, args.limit)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram recall")
    parser.add_argument("query", nargs="?", default=None, help="FTS search query.")
    parser.add_argument("--limit", type=int, default=20, help="Max results (default 20).")
    parser.add_argument("--stats", action="store_true", help="Show summary counts.")
    parser.add_argument("--id", type=int, default=None, help="Show full detail for one memory.")
    return parser.parse_args(argv)


def _list_all(conn, limit: int) -> int:
    mems = memory_store.list_memories(conn, order="created")[:limit]
    print(json.dumps({"count": len(mems), "memories": [_summary(m) for m in mems]}))
    return 0


def _search(conn, query: str, limit: int) -> int:
    if not memory_store.fts_quote(query):
        return _list_all(conn, limit)
    mems = memory_store.search(conn, query, limit)
    print(json.dumps({"query": query, "count": len(mems),
                      "memories": [_summary(m) for m in mems]}))
    return 0


def _show_detail(conn, memory_id: int) -> int:
    mem = memory_store.get(conn, memory_id)
    if not mem:
        print(json.dumps({"error": "not_found", "id": memory_id}))
        return 1

    triggers = memory_store.triggers_for(conn, memory_id)
    surfaces = conn.execute(
        "SELECT session_id, hook, surfaced_ts FROM session_surfaces "
        "WHERE memory_id = ? ORDER BY surfaced_ts DESC LIMIT 10",
        (memory_id,),
    ).fetchall()

    print(json.dumps({
        "memory": dataclasses.asdict(mem),
        "triggers": [_trigger_dict(t) for t in triggers],
        "recent_surfaces": [dict(s) for s in surfaces],
    }))
    return 0


def _show_stats(conn) -> int:
    print(json.dumps(memory_store.summary_stats(conn)))
    return 0


def _summary(m: Memory) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "kind": m.kind,
        "scope": m.scope,
        "surface_count": m.surface_count,
        "useful_count": m.useful_count,
        "pinned": m.pinned,
    }


def _trigger_dict(t: Trigger) -> dict:
    return {
        "kind": t.kind,
        "first_token": t.first_token,
        "tokens_json": t.tokens_json,
        "path_pattern": t.path_pattern,
    }
