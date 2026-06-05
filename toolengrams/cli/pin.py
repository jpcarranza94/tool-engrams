"""Formation CLI: `engram pin` — pin/unpin a memory.

Pinned memories get a 1.5× boost in the scoring formula and are always
injected at session start, regardless of reinforcement score.
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import db, memory_store


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.name:
        print("engram pin: provide a memory name", file=sys.stderr)
        return 2

    with db.session() as conn:
        mem = memory_store.find_by_name(conn, args.name)
        if not mem:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1

        new_pinned = not args.unpin
        memory_store.set_pinned(conn, mem.id, new_pinned)

        print(json.dumps({
            "action": "unpinned" if args.unpin else "pinned",
            "memory_id": mem.id,
            "name": mem.name,
            "pinned": new_pinned,
        }))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram pin")
    parser.add_argument("name", nargs="?", default=None, help="Memory name.")
    parser.add_argument("--unpin", action="store_true", help="Unpin instead of pin.")
    return parser.parse_args(argv)
