"""UserPromptSubmit hook command — watcher liveness safety check.

Fires once per user message. Checks if the watcher cron is alive for this
session. If it died (process no longer exists), respawns it.

Input JSON on stdin:
    {
      "session_id": "...",
      "cwd": "..."
    }

Output: {} (no injection)
"""

from __future__ import annotations

import json
import os
import sys

from .. import db
from ..watcher import derive_transcript_path, spawn_watcher


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _emit({})
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    if not session_id:
        _emit({})
        return 0

    try:
        _ensure_watcher_alive(session_id, cwd)
    except Exception:
        pass

    _emit({})
    return 0


def _ensure_watcher_alive(session_id: str, cwd: str) -> None:
    """If watcher died, respawn it."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT watcher_pid, transcript_path FROM watcher_state "
            "WHERE work_session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return  # no watcher started (session predates watcher feature)
        if row["watcher_pid"] and _is_pid_alive(row["watcher_pid"]):
            return  # watcher is fine
        # Watcher died -- respawn.
        transcript_path = row["transcript_path"] or derive_transcript_path(session_id, cwd)
        spawn_watcher(session_id, transcript_path, cwd)
    finally:
        conn.close()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
