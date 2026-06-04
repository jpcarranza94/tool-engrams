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

from ..prompts.session_start import FORMATION_GUIDANCE
from ..utils import is_watcher_child
from ..watcher import derive_transcript_path, tick
from ._skip import is_internal_cwd


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    try:
        _ensure_session_tracked(payload)
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


def _ensure_session_tracked(payload: dict) -> None:
    """Register the session in watcher_state so event-driven ticks have a cursor
    and config to read. No long-running process is spawned — ticks fire from the
    Stop / SessionEnd / failure→success / user-correction hooks.

    Also runs the idle-sweep: re-fire a flush tick for any *other* tracked
    session whose tail was left unprocessed (it died before its final
    Stop/flush). A new session starting is a cheap, reliable moment to catch up
    on abandoned ones."""
    # A watcher-launched `claude` must not register/trigger watchers (recursion).
    if is_watcher_child():
        return
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    if not session_id or not cwd:
        return
    # Skip non-user sessions (consolidation agent, old observer, etc.)
    if is_internal_cwd(cwd):
        return
    transcript_path = derive_transcript_path(session_id, cwd)
    tick.ensure_row(session_id, transcript_path, cwd)
    tick.sweep_idle_sessions(session_id)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
