"""Formation CLI: `engram verify` — mark a memory as still accurate.

Used by the nightly consolidation agent after it has checked a memory's
body against current reality (git log, file contents) and decided the
memory still holds. Sets memories.last_verified_ts = NOW.

Pairs with `engram forget --delete` which archives memories whose body
contradicts current reality. Together they let consolidation skip
recently-verified memories on subsequent runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .. import db
from ..queries import find_memory


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        row = find_memory(conn, args.name, include_archived=True)
        if not row:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1
        if row["archived_ts"] is not None:
            print(json.dumps({"error": "archived", "memory_id": row["id"], "name": row["name"]}))
            return 1
        now_ts = int(time.time())
        conn.execute(
            "UPDATE memories SET last_verified_ts = ? WHERE id = ?",
            (now_ts, row["id"]),
        )
        print(json.dumps({
            "action": "verified",
            "memory_id": row["id"],
            "name": row["name"],
            "last_verified_ts": now_ts,
        }))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram verify")
    parser.add_argument("name", help="Memory name (exact or fuzzy match).")
    return parser.parse_args(argv)
