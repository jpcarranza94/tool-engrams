"""Formation CLI: `engram skip <name>` — mark a surfaced memory as unused.

Negative reinforcement: when Claude (or any caller) sees a hint surface and
decides it doesn't apply, it can run `engram skip <name>` to flag the most
recent unmarked surface of that memory in the current session with
outcome='unused'. The consolidation agent uses unused/noise/helpful ratios
to identify memories worth pruning.

Session resolution:
  1. $CLAUDE_SESSION_ID env var (preferred — when Claude Code propagates it).
  2. Fallback: newest session with surface activity in the last hour.
  3. Failing both: error out.
"""

from __future__ import annotations

import argparse
import json
import os

from .. import db
from ..queries import find_memory
from ..retrieval.session_state import (
    find_active_session,
    get_most_recent_unmarked_surface,
    mark_surface_outcome_by_ts,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        row = find_memory(conn, args.name, include_archived=True)
        if not row:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1

        session_id = args.session_id or os.environ.get("CLAUDE_SESSION_ID")
        if not session_id:
            session_id = find_active_session(conn)
        if not session_id:
            print(json.dumps({
                "error": "no_active_session",
                "message": "No $CLAUDE_SESSION_ID set and no recent session activity. "
                           "Pass --session-id to target a specific session.",
            }))
            return 1

        surfaced_ts = get_most_recent_unmarked_surface(conn, session_id, row["id"])
        if surfaced_ts is None:
            print(json.dumps({
                "action": "noop",
                "reason": "no_unmarked_surface_in_session",
                "memory_id": row["id"],
                "name": row["name"],
                "session_id": session_id,
            }))
            return 0

        with db.transaction(conn):
            updated = mark_surface_outcome_by_ts(
                conn, session_id, row["id"], surfaced_ts, "unused",
            )
        if not updated:
            # Lost a race with another process — surface row got marked between
            # our SELECT and UPDATE. Treat as a noop rather than an error.
            print(json.dumps({
                "action": "noop",
                "reason": "race_lost",
                "memory_id": row["id"],
                "name": row["name"],
                "session_id": session_id,
            }))
            return 0

        print(json.dumps({
            "action": "skipped",
            "memory_id": row["id"],
            "name": row["name"],
            "session_id": session_id,
            "surfaced_ts": surfaced_ts,
            "outcome": "unused",
        }))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram skip")
    parser.add_argument("name", help="Memory name (exact or fuzzy match).")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Explicit session_id. Defaults to $CLAUDE_SESSION_ID or the "
             "newest session with recent surface activity.",
    )
    return parser.parse_args(argv)
