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
import sys
import time

from .. import db
from ..utils import is_pid_alive
from ..watcher import WATCHER_INTERVAL, derive_transcript_path, spawn_watcher
from ._skip import is_internal_cwd

# How long since the watcher's last cron tick before we treat it as dead
# even if its PID is still around (zombie / stuck on `time.sleep`-after-fork).
# Two intervals is the right shape: one missed tick is normal jitter; two
# means something is genuinely wrong.
_STALE_TICK_GRACE_SEC = WATCHER_INTERVAL * 2


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
    """If watcher is dead or was never started, spawn it."""
    if not cwd:
        return

    # Skip ToolEngrams' own subprocess sessions (consolidation agent, etc.).
    # Without this, the consolidation agent's `claude -p` subprocess would
    # cause its own UserPromptSubmit hook to spawn a watcher on its temp
    # transcript — heavy irrelevant deltas + wasted model calls.
    if is_internal_cwd(cwd):
        return

    with db.session() as conn:
        row = conn.execute(
            "SELECT watcher_pid, transcript_path, last_checked_ts "
            "FROM watcher_state WHERE work_session_id = ?",
            (session_id,),
        ).fetchone()
    if _is_watcher_healthy(row):
        return
    # Watcher either never existed, died, or is stuck — spawn a fresh one.
    transcript_path = (row["transcript_path"] if row else None) or derive_transcript_path(session_id, cwd)
    spawn_watcher(session_id, transcript_path, cwd)


def _is_watcher_healthy(row) -> bool:
    """A watcher is healthy iff its PID is alive AND its last tick is recent.

    The last_checked_ts check catches zombies: PID still exists but the
    process is wedged (e.g. blocked on a never-returning subprocess.run, or
    the cron loop hung mid-iteration). If we don't see a recent heartbeat,
    treat it as dead and let spawn_watcher replace it.
    """
    if row is None:
        return False
    if not is_pid_alive(row["watcher_pid"]):
        return False
    last_checked = row["last_checked_ts"] or 0
    if last_checked and time.time() - last_checked > _STALE_TICK_GRACE_SEC:
        return False
    return True


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
