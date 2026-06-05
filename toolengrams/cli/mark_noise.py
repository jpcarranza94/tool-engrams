"""Reinforcement CLI: `engram mark-noise <name>` — retroactively label a
memory's surfaces as noise.

Used by the nightly consolidation agent when reviewing yesterday's traces
and concluding that a memory's past surfaces were noise even though no
in-session `engram skip` ran. Centralizes the CHECK-constraint write so
prompts don't have to bake in raw `UPDATE session_surfaces SET outcome='noise'`
SQL.

By default marks ALL unmarked surfaces of the memory. Pass `--session-id` to
scope to a single session.
"""

from __future__ import annotations

import argparse
import json
import logging

from .. import db, memory_store
from ..retrieval import session_state

logger = logging.getLogger("engram.mark_noise")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        mem = memory_store.find_by_name(conn, args.name, include_archived=True)
        if not mem:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1

        with db.transaction(conn):
            updated = session_state.mark_unmarked_noise(conn, mem.id, args.session_id)

        if updated == 0:
            print(json.dumps({
                "action": "noop",
                "reason": "no_unmarked_surfaces",
                "memory_id": mem.id,
                "name": mem.name,
                "session_id": args.session_id,
            }))
            return 0

        logger.info(
            "marked outcome=noise memory_id=%d session=%s rows=%d",
            mem.id, args.session_id or "*", updated,
        )
        print(json.dumps({
            "action": "marked_noise",
            "memory_id": mem.id,
            "name": mem.name,
            "session_id": args.session_id,
            "rows_updated": updated,
        }))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram mark-noise")
    parser.add_argument("name", help="Memory name (exact or fuzzy match).")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional: scope to a single session. Default: all sessions.",
    )
    return parser.parse_args(argv)
