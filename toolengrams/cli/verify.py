"""Formation CLI: `engram verify` — mark a memory as still accurate.

Used by the nightly consolidation agent after auditing a memory's body against
current reality (git log, file contents) and deciding the memory still holds.
Sets memories.last_verified_ts = NOW.

Pairs with `engram forget --delete` which archives memories whose body
contradicts current reality. Together they let consolidation skip
recently-verified memories on subsequent runs.

No-op guard: if last_verified_ts is within NOOP_WINDOW_SECONDS, returns
action=noop and does not write. Prevents FTS-trigger churn from same-second
duplicate verifies (e.g. agent retries after a transient git error).
"""

from __future__ import annotations

import argparse
import json
import time

from .. import db, memory_store

NOOP_WINDOW_SECONDS = 60


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        mem = memory_store.find_by_name(conn, args.name, include_archived=True)
        if not mem:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1
        if mem.archived_ts is not None:
            print(json.dumps({
                "error": "archived",
                "memory_id": mem.id,
                "name": mem.name,
            }))
            return 2

        now_ts = int(time.time())
        previous = mem.last_verified_ts

        if previous is not None and now_ts - previous < NOOP_WINDOW_SECONDS:
            print(json.dumps({
                "action": "noop",
                "reason": "recently_verified",
                "memory_id": mem.id,
                "name": mem.name,
                "previous_last_verified_ts": previous,
            }))
            return 0

        with db.transaction(conn):
            memory_store.set_verified(conn, mem.id, now_ts)
        print(json.dumps({
            "action": "verified",
            "memory_id": mem.id,
            "name": mem.name,
            "last_verified_ts": now_ts,
            "previous_last_verified_ts": previous,
        }))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram verify")
    parser.add_argument("name", help="Memory name (exact or fuzzy match).")
    return parser.parse_args(argv)
