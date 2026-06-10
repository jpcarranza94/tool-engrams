"""SessionStart hook command.

Injects formation guidance: tells Claude how and when to use `engram remember`
to form tool-bound memories. Also registers the session in watcher_state (so the
event-driven ticks have a cursor), runs the idle-sweep that recovers tails of
sessions that died before their final flush, and at most once a day spawns the
detached residue cleanup. Everything in-hook is cheap; model work and filesystem
sweeps happen in detached processes.

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
import os
import sys
from pathlib import Path

from .. import pause
from ..prompts.session_start import FORMATION_GUIDANCE
from ..utils import is_watcher_child
from ..watcher import cleanup, derive_transcript_path, tick
from ._skip import is_internal_cwd


def main() -> int:
    if pause.is_disabled():
        _emit({})
        return 0
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
                "additionalContext": FORMATION_GUIDANCE + _double_install_warning(),
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
    # Once a day (marker-gated; one stat on the common path), reap cold watcher
    # residue in a detached process: dead watcher_state rows, stale sandbox
    # cwds, and the watcher sessions' own old transcripts.
    cleanup.maybe_spawn_cleanup()


def _double_install_warning() -> str:
    """Plugin + legacy script installs wire the same hooks — detect and warn.

    Only the plugin shim sets ENGRAM_PLUGIN; legacy hooks in settings.json are
    plain `engram <subcommand>` commands. Both present means every hook fires
    twice. install.sh refuses in the other direction; this covers users who
    installed the plugin without running `install.sh --uninstall` first.
    """
    if os.environ.get("ENGRAM_PLUGIN") != "1":
        return ""
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        for entries in (settings.get("hooks") or {}).values():
            for entry in entries:
                for hook in entry.get("hooks") or []:
                    if str(hook.get("command", "")).startswith("engram "):
                        return (
                            "\n\n[ToolEngrams WARNING] Both the plugin AND the legacy "
                            "script install are active — every hook fires twice. "
                            "Fix: run ./install.sh --uninstall in the tool-engrams "
                            "repo (keeps the DB), or remove the engram hook entries "
                            "from ~/.claude/settings.json."
                        )
    except Exception:
        pass  # advisory only — never block the hook
    return ""


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
