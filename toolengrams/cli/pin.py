"""Formation CLI: `engram pin` — pin/unpin a memory.

Pinned memories get a 1.5× boost in the scoring formula and are always
injected at session start, regardless of reinforcement score.
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import db
from ..queries import find_memory


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.name:
        print("engram pin: provide a memory name", file=sys.stderr)
        return 2

    with db.session() as conn:
        row = find_memory(conn, args.name)
        if not row:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1

        new_pinned = 0 if args.unpin else 1
        conn.execute("UPDATE memories SET pinned = ? WHERE id = ?", (new_pinned, row["id"]))

        print(json.dumps({
            "action": "unpinned" if args.unpin else "pinned",
            "memory_id": row["id"],
            "name": row["name"],
            "pinned": bool(new_pinned),
        }))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram pin")
    parser.add_argument("name", nargs="?", default=None, help="Memory name.")
    parser.add_argument("--unpin", action="store_true", help="Unpin instead of pin.")
    return parser.parse_args(argv)
