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
import sqlite3

from .. import db
from ..queries import find_memory

logger = logging.getLogger("engram.mark_noise")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        row = find_memory(conn, args.name, include_archived=True)
        if not row:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1

        with db.transaction(conn):
            updated = _mark_unmarked_surfaces_noise(
                conn, row["id"], args.session_id,
            )

        if updated == 0:
            print(json.dumps({
                "action": "noop",
                "reason": "no_unmarked_surfaces",
                "memory_id": row["id"],
                "name": row["name"],
                "session_id": args.session_id,
            }))
            return 0

        logger.info(
            "marked outcome=noise memory_id=%d session=%s rows=%d",
            row["id"], args.session_id or "*", updated,
        )
        print(json.dumps({
            "action": "marked_noise",
            "memory_id": row["id"],
            "name": row["name"],
            "session_id": args.session_id,
            "rows_updated": updated,
        }))
        return 0


def _mark_unmarked_surfaces_noise(
    conn: sqlite3.Connection,
    memory_id: int,
    session_id: str | None,
) -> int:
    sql = (
        "UPDATE session_surfaces SET outcome = 'noise' "
        "WHERE memory_id = ? AND outcome IS NULL"
    )
    params: list = [memory_id]
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    cur = conn.execute(sql, params)
    return cur.rowcount or 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram mark-noise")
    parser.add_argument("name", help="Memory name (exact or fuzzy match).")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional: scope to a single session. Default: all sessions.",
    )
    return parser.parse_args(argv)
