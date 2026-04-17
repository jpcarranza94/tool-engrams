"""SessionStart hook command.

Injects formation guidance: tells Claude how and when to use `engram remember`
to form tool-bound memories. Also spawns the persistent watcher cron for
automatic memory formation.

Input JSON on stdin (subset):
    {
      "session_id": "...",
      "cwd": "...",
      "hook_event_name": "SessionStart",
      "source": "startup" | "resume" | "clear" | "compact"
    }

Output:
    {
      "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "..."
      }
    }
"""

from __future__ import annotations

import json
import sys

from .. import db
from ..prompts.session_start import FORMATION_GUIDANCE
from ..watcher import derive_transcript_path, spawn_watcher


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    try:
        _maybe_spawn_watcher(payload)
    except Exception:
        pass  # watcher is best-effort -- never block the hook

    try:
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": FORMATION_GUIDANCE,
            }
        })
        return 0
    except Exception as e:  # pragma: no cover
        print(f"engram session-start: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _maybe_spawn_watcher(payload: dict) -> None:
    """Spawn watcher on startup; check existing on resume; skip clear/compact."""
    source = payload.get("source", "")
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")

    if not session_id or not cwd:
        return

    if source == "startup":
        transcript_path = derive_transcript_path(session_id, cwd)
        spawn_watcher(session_id, transcript_path, cwd)
    elif source == "resume":
        # Check if watcher already exists and is alive.
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT watcher_pid FROM watcher_state WHERE work_session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                # No watcher -- spawn one.
                transcript_path = derive_transcript_path(session_id, cwd)
                spawn_watcher(session_id, transcript_path, cwd)
        finally:
            conn.close()
    # clear / compact: do nothing


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
