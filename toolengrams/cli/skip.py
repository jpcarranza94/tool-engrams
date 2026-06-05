"""Reinforcement CLI: `engram skip <name>` — mark a surfaced memory as unused.

Negative reinforcement: when Claude (or any caller) sees a hint surface and
decides it doesn't apply, it can run `engram skip <name>` to flag the most
recent unmarked surface of that memory in the current session with
outcome='unused'. The consolidation agent uses unused/noise/helpful ratios
to identify memories worth pruning.

Session resolution (in order; first hit wins):
  1. `--session-id <ID>` explicit flag.
  2. `$CLAUDE_SESSION_ID` env var (when Claude Code propagates it).
  3. `--latest-session` opt-in: newest session with tool-call activity in the
     last hour. Off by default — this CLI mutates state, and silently
     defaulting to "newest session, hope it's mine" is unsafe when multiple
     Claude windows are open.
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from .. import db, memory_store
from ..retrieval.session_state import (
    find_latest_active_session,
    get_most_recent_unmarked_surface,
    mark_surface_outcome_by_ts,
)

logger = logging.getLogger("engram.skip")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with db.session() as conn:
        # Archived memories aren't skippable — they can no longer surface, so
        # marking outcome is meaningless. Surface as not_found to keep the
        # caller's mental model clean (symmetric with `engram verify`'s
        # treatment of non-existent names, plus a clear `error` field).
        mem = memory_store.find_by_name(conn, args.name, include_archived=False)
        if not mem:
            print(json.dumps({"error": "not_found", "query": args.name}))
            return 1

        session_id, resolved_via = _resolve_session(conn, args)
        if not session_id:
            print(json.dumps({
                "error": "no_session",
                "message": (
                    "Pass --session-id, set $CLAUDE_SESSION_ID, or opt in to "
                    "--latest-session. The skip CLI does NOT silently default "
                    "to the newest session because it mutates reinforcement "
                    "state and the wrong session could be picked when multiple "
                    "Claude windows are open."
                ),
            }))
            return 1

        surfaced_ts = get_most_recent_unmarked_surface(conn, session_id, mem.id)
        if surfaced_ts is None:
            print(json.dumps({
                "action": "noop",
                "reason": "no_unmarked_surface_in_session",
                "memory_id": mem.id,
                "name": mem.name,
                "session_id": session_id,
                "resolved_via": resolved_via,
            }))
            return 0

        with db.transaction(conn):
            updated = mark_surface_outcome_by_ts(
                conn, session_id, mem.id, surfaced_ts, "unused",
            )
        if not updated:
            # Lost a race with another process — surface row got marked between
            # our SELECT and UPDATE. Treat as a noop rather than an error.
            print(json.dumps({
                "action": "noop",
                "reason": "race_lost",
                "memory_id": mem.id,
                "name": mem.name,
                "session_id": session_id,
                "resolved_via": resolved_via,
            }))
            return 0

        logger.info(
            "marked outcome=unused memory_id=%d session=%s surfaced_ts=%d resolved_via=%s",
            mem.id, session_id, surfaced_ts, resolved_via,
        )
        print(json.dumps({
            "action": "skipped",
            "memory_id": mem.id,
            "name": mem.name,
            "session_id": session_id,
            "surfaced_ts": surfaced_ts,
            "outcome": "unused",
            "resolved_via": resolved_via,
        }))
        return 0


def _resolve_session(conn, args) -> tuple[str | None, str | None]:
    """Return (session_id, resolved_via). resolved_via is one of:
    'flag', 'env', 'latest-session-flag', or None when nothing resolved.
    """
    if args.session_id:
        return args.session_id, "flag"
    env_sid = os.environ.get("CLAUDE_SESSION_ID")
    if env_sid:
        return env_sid, "env"
    if args.latest_session:
        sid = find_latest_active_session(conn)
        if sid:
            return sid, "latest-session-flag"
    return None, None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram skip")
    parser.add_argument("name", help="Memory name (exact or fuzzy match).")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Explicit session_id to target.",
    )
    parser.add_argument(
        "--latest-session",
        action="store_true",
        help="Fall back to the newest session with tool-call activity in the "
             "last hour if --session-id and $CLAUDE_SESSION_ID are both unset. "
             "Opt-in: do not use from interactive contexts.",
    )
    return parser.parse_args(argv)
