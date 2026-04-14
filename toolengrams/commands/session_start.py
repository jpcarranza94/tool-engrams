"""SessionStart hook command.

Injects formation guidance: tells Claude how and when to use `engram remember`
to form tool-bound memories. This is the only job of SessionStart — all
memory surfacing happens via PreToolUse when the matching tool call fires.

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


def main() -> int:
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


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
