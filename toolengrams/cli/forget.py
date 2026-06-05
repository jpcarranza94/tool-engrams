"""Formation CLI: `engram forget` — soft-demote or archive memories.

Per design-v8.md §8:
  - `engram forget <name>`            → soft demote (useful_count=0, surface_count+=5, last_surfaced_ts=0)
  - `engram forget --delete <name>`   → set archived_ts, excluded from retrieval
  - `engram forget --topic <keyword>` → soft-demote all matching by FTS
  - `engram forget --restore <name>`  → undo soft demote (reset surface_count=0, useful_count=0)

Name lookup is fuzzy: exact match first, then FTS MATCH, then LIKE.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .. import db, memory_store


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        if args.topic:
            return _forget_topic(conn, args.topic, args.delete)
        if args.restore:
            return _restore(conn, args.restore)
        if not args.name:
            print("engram forget: provide a memory name, --topic, or --restore", file=sys.stderr)
            return 2
        return _forget_one(conn, args.name, args.delete)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram forget")
    parser.add_argument("name", nargs="?", default=None,
                        help="Memory name (exact or fuzzy match).")
    parser.add_argument("--delete", action="store_true",
                        help="Hard archive instead of soft demote.")
    parser.add_argument("--topic", default=None,
                        help="Soft-demote all memories matching this keyword via FTS.")
    parser.add_argument("--restore", default=None, metavar="NAME",
                        help="Undo a soft demote or archive.")
    return parser.parse_args(argv)


# ---------- actions ----------


def _forget_one(conn, name: str, hard_delete: bool) -> int:
    mem = memory_store.find_by_name(conn, name)
    if not mem:
        print(json.dumps({"error": "not_found", "query": name}))
        return 1

    if hard_delete:
        memory_store.archive(conn, mem.id)
        action = "archived"
    else:
        memory_store.soft_demote(conn, mem.id)
        action = "soft_demoted"

    print(json.dumps({
        "action": action,
        "memory_id": mem.id,
        "name": mem.name,
    }))
    return 0


def _forget_topic(conn, keyword: str, hard_delete: bool) -> int:
    # Effectively unbounded — the store is tiny, and search() orders by FTS rank
    # so if a topic ever exceeded this the least-relevant matches drop first.
    mems = memory_store.search(conn, keyword, limit=10000)
    if not mems:
        print(json.dumps({"error": "no_matches", "topic": keyword}))
        return 1

    now_ts = int(time.time())
    affected = []
    for m in mems:
        if hard_delete:
            memory_store.archive(conn, m.id, now_ts)
        else:
            memory_store.soft_demote(conn, m.id)
        affected.append({"memory_id": m.id, "name": m.name})

    print(json.dumps({
        "action": "archived" if hard_delete else "soft_demoted",
        "topic": keyword,
        "count": len(affected),
        "memories": affected,
    }))
    return 0


def _restore(conn, name: str) -> int:
    mem = memory_store.find_by_name(conn, name, include_archived=True)
    if not mem:
        print(json.dumps({"error": "not_found", "query": name}))
        return 1

    memory_store.restore(conn, mem.id)
    print(json.dumps({
        "action": "restored",
        "memory_id": mem.id,
        "name": mem.name,
    }))
    return 0
