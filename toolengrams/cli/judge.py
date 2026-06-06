"""Reinforcement CLI: `engram judge <memory_id> <helpful|unused|noise>`.

The evaluation watcher's one verb. It labels how a surfaced memory fared on the
call it surfaced on, reading the model's *forward* actions:

    helpful → the model visibly followed the memory   → useful_count++
    unused  → the memory was relevant, not acted on    → (neither counter)
    noise   → the memory had no bearing; the trigger over-matched → noise_count++

With no constrained JSON schema, this CLI is the validation boundary (design-v10
§3.5). It rejects an unknown memory_id, a memory_id not surfaced in this session,
and an outcome outside the set. It is idempotent — `mark_surface_outcome` only
writes rows whose outcome is NULL, so a retry that re-judges a closed surface is
a noop and never double-bumps the counter. The outcome write and the counter
bump land in one transaction.

Session resolution: `--session-id` flag, else `$CLAUDE_SESSION_ID`. Unlike
`engram skip`, there is no `--latest-session` guess — the eval watcher always
knows the work session id and passes it explicitly.
"""

from __future__ import annotations

import argparse
import json
import os

from .. import db, memory_store
from ..retrieval import session_state
from ..watcher.log import _log

_OUTCOMES = ("helpful", "unused", "noise")
_BUMP = {"helpful": memory_store.bump_useful, "noise": memory_store.bump_noise}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.outcome not in _OUTCOMES:
        print(json.dumps({
            "error": "bad_outcome", "outcome": args.outcome, "allowed": list(_OUTCOMES),
        }))
        return 1

    session_id = args.session_id or os.environ.get("CLAUDE_SESSION_ID") or ""
    if not session_id:
        print(json.dumps({
            "error": "no_session",
            "message": "Pass --session-id or set $CLAUDE_SESSION_ID.",
        }))
        return 1

    with db.session() as conn:
        mem = memory_store.get(conn, args.memory_id)
        if mem is None:
            print(json.dumps({"error": "not_found", "memory_id": args.memory_id}))
            return 1

        # Distinguish "never surfaced here" (reject) from "already judged" (noop).
        if not session_state.has_surface(conn, session_id, args.memory_id):
            print(json.dumps({
                "error": "not_in_session",
                "memory_id": args.memory_id, "session_id": session_id,
            }))
            return 1

        with db.transaction(conn):
            updated = session_state.mark_surface_outcome(
                conn, session_id, [args.memory_id], args.outcome,
            )
            # One verdict = one counter bump, only when a pending surface closed.
            if updated and args.outcome in _BUMP:
                _BUMP[args.outcome](conn, [args.memory_id])

    if not updated:
        _log(f"JUDGE-NOOP memory_id={args.memory_id} session={session_id} "
             f"outcome={args.outcome} reason=already_judged")
        print(json.dumps({
            "action": "noop", "reason": "already_judged",
            "memory_id": args.memory_id, "session_id": session_id,
        }))
        return 0

    _log(f"JUDGE memory_id={args.memory_id} session={session_id} "
         f"outcome={args.outcome} rows={updated}")
    print(json.dumps({
        "action": "judged", "memory_id": args.memory_id, "name": mem.name,
        "outcome": args.outcome, "session_id": session_id, "rows_updated": updated,
    }))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram judge")
    parser.add_argument("memory_id", type=int, help="The memory's integer id.")
    parser.add_argument(
        "outcome",
        help="How the surfaced memory fared: helpful | unused | noise.",
    )
    parser.add_argument(
        "--session-id", default=None,
        help="Session whose surfaces to judge. Defaults to $CLAUDE_SESSION_ID.",
    )
    return parser.parse_args(argv)
