"""PostToolUseFailure hook — hint injection on tool-call failure.

Fires only when a tool call has actually failed (Claude Code's PostToolUseFailure
event). This event is the right surface moment for hints: Claude Code already
discriminates real failures from semantically-OK non-zero exits (e.g. grep
no-match), so we don't have to sniff exit codes.

Empirical payload shape (verified 2026-04-21 across Bash/Read/Edit):
    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "PostToolUseFailure",
      "tool_name": "Bash",
      "tool_input": {"command": "..."},
      "tool_use_id": "...",
      "error": "Exit code 1" | "File does not exist..." | ...,
      "is_interrupt": false
    }

No `tool_response` — the tool failed, nothing returned.

Behavior:
  - Skip if `is_interrupt` (user interrupted, not a real tool failure).
  - Retrieve memories with kind='hint' whose triggers match the failed call.
  - Session dedup against already-surfaced memories.
  - Emit `additionalContext` on hookSpecificOutput. No `permissionDecision` —
    PostToolUseFailure cannot block; the call already failed.
  - Log surfaces with hook='post_tool_use_failure', bump surface_count.

Fails open: any exception → exit 0 with `{}` so the hint layer never interferes.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .. import pause
from ..target import get_target
from ._failure_surface import surface_failure_hints


def main(target_name: str = "claude-code") -> int:
    if pause.is_disabled():
        _emit({})
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"engram post-tool-failure: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload, get_target(target_name))
    except Exception as e:  # pragma: no cover - fail-open safety net
        print(f"engram post-tool-failure: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any], target) -> int:
    _emit(surface_failure_hints(payload, target,
                                output_event_name="PostToolUseFailure"))
    return 0


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
