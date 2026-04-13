"""PostToolUse hook command — success reinforcement.

After a tool call completes, look up which memories were surfaced for that
tool_use_id during the PreToolUse phase. If the tool call succeeded (no error),
bump useful_count on those memories. This is the positive feedback loop that
lets good memories strengthen over time.

Input JSON on stdin (subset):
    {
      "session_id": "...",
      "cwd": "...",
      "hook_event_name": "PostToolUse",
      "tool_name": "Bash",
      "tool_use_id": "toolu_abc123",
      "tool_response": "command output...",
      "tool_input": {"command": "git status"},
      "is_error": false
    }

Output: {} (no injection — reinforcement is silent).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .. import db


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"engram post-tool: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover
        print(f"engram post-tool: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any]) -> int:
    tool_use_id = payload.get("tool_use_id") or ""
    session_id = payload.get("session_id") or ""
    is_error = _detect_error(payload)

    if not tool_use_id or not session_id:
        _emit({})
        return 0

    # Only reinforce on success — errors are neutral (the memory may have
    # been trying to help avoid the error).
    if is_error:
        _emit({})
        return 0

    conn = db.connect()
    try:
        # Find memories that were surfaced for this exact tool call.
        rows = conn.execute(
            "SELECT memory_id FROM session_surfaces "
            "WHERE session_id = ? AND tool_use_id = ? AND hook = 'pre_tool_use'",
            (session_id, tool_use_id),
        ).fetchall()

        if not rows:
            _emit({})
            return 0

        memory_ids = [r["memory_id"] for r in rows]
        placeholders = ",".join("?" * len(memory_ids))
        conn.execute(
            f"UPDATE memories SET useful_count = useful_count + 1 "
            f"WHERE id IN ({placeholders})",
            memory_ids,
        )

        _emit({})
        return 0
    finally:
        conn.close()


def _detect_error(payload: dict[str, Any]) -> bool:
    """Determine if the tool call failed.

    Claude Code provides is_error directly in some cases. For Bash, we also
    check for non-zero exit codes or stderr markers in the response.
    """
    if payload.get("is_error"):
        return True

    response = payload.get("tool_response") or ""
    if isinstance(response, str):
        # Claude Code wraps Bash errors in an <error> tag or prefixes with "Exit code"
        if response.startswith("<error>") or "Exit code" in response[:50]:
            return True

    return False


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
