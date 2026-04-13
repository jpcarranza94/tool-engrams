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

from .. import db


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    conn = db.connect()
    try:
        if args.topic:
            return _forget_topic(conn, args.topic, args.delete)
        if args.restore:
            return _restore(conn, args.restore)
        if not args.name:
            print("engram forget: provide a memory name, --topic, or --restore", file=sys.stderr)
            return 2
        return _forget_one(conn, args.name, args.delete)
    finally:
        conn.close()


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


# ---------- lookup ----------


def _find_memory(conn, name: str):
    """Find a memory by exact name, then FTS, then LIKE. Returns Row or None."""
    row = conn.execute(
        "SELECT id, name, surface_count, useful_count, archived_ts "
        "FROM memories WHERE name = ? AND archived_ts IS NULL",
        (name,),
    ).fetchone()
    if row:
        return row

    # FTS match
    rows = conn.execute(
        "SELECT m.id, m.name, m.surface_count, m.useful_count, m.archived_ts "
        "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND m.archived_ts IS NULL "
        "ORDER BY rank LIMIT 1",
        (_fts_quote(name),),
    ).fetchall()
    if rows:
        return rows[0]

    # LIKE fallback
    row = conn.execute(
        "SELECT id, name, surface_count, useful_count, archived_ts "
        "FROM memories WHERE name LIKE ? AND archived_ts IS NULL LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    return row


def _find_memory_including_archived(conn, name: str):
    """Like _find_memory but also searches archived memories (for --restore)."""
    row = conn.execute(
        "SELECT id, name, surface_count, useful_count, archived_ts "
        "FROM memories WHERE name = ?",
        (name,),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT id, name, surface_count, useful_count, archived_ts "
        "FROM memories WHERE name LIKE ? LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    return row


def _fts_quote(text: str) -> str:
    tokens = text.split()
    return " OR ".join(f'"{t}"' for t in tokens if t)


# ---------- actions ----------


def _forget_one(conn, name: str, hard_delete: bool) -> int:
    row = _find_memory(conn, name)
    if not row:
        print(json.dumps({"error": "not_found", "query": name}))
        return 1

    if hard_delete:
        conn.execute(
            "UPDATE memories SET archived_ts = ? WHERE id = ?",
            (int(time.time()), row["id"]),
        )
        action = "archived"
    else:
        conn.execute(
            "UPDATE memories SET useful_count = 0, "
            "surface_count = surface_count + 5, last_surfaced_ts = 0 "
            "WHERE id = ?",
            (row["id"],),
        )
        action = "soft_demoted"

    print(json.dumps({
        "action": action,
        "memory_id": row["id"],
        "name": row["name"],
    }))
    return 0


def _forget_topic(conn, keyword: str, hard_delete: bool) -> int:
    rows = conn.execute(
        "SELECT m.id, m.name FROM memories m "
        "JOIN memories_fts f ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND m.archived_ts IS NULL",
        (_fts_quote(keyword),),
    ).fetchall()

    if not rows:
        print(json.dumps({"error": "no_matches", "topic": keyword}))
        return 1

    now_ts = int(time.time())
    affected = []
    for r in rows:
        if hard_delete:
            conn.execute("UPDATE memories SET archived_ts = ? WHERE id = ?", (now_ts, r["id"]))
        else:
            conn.execute(
                "UPDATE memories SET useful_count = 0, "
                "surface_count = surface_count + 5, last_surfaced_ts = 0 "
                "WHERE id = ?",
                (r["id"],),
            )
        affected.append({"memory_id": r["id"], "name": r["name"]})

    print(json.dumps({
        "action": "archived" if hard_delete else "soft_demoted",
        "topic": keyword,
        "count": len(affected),
        "memories": affected,
    }))
    return 0


def _restore(conn, name: str) -> int:
    row = _find_memory_including_archived(conn, name)
    if not row:
        print(json.dumps({"error": "not_found", "query": name}))
        return 1

    conn.execute(
        "UPDATE memories SET archived_ts = NULL, "
        "useful_count = 0, surface_count = 0, last_surfaced_ts = 0 "
        "WHERE id = ?",
        (row["id"],),
    )
    print(json.dumps({
        "action": "restored",
        "memory_id": row["id"],
        "name": row["name"],
    }))
    return 0
