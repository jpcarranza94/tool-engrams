"""PostToolUse hook command — success reinforcement.

Bumps useful_count for memories that were surfaced on this tool call and
increments the per-session turn counter.

Output: {} (no injection).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .. import db
from ..reinforcement.counters import bump_useful_counts
from ..retrieval.session_state import (
    get_tool_call_surfaces,
    increment_session_turn,
)


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

    now_ts = int(time.time())
    conn = db.connect()
    try:
        # Reinforcement: only on success. Target PRIMARY surfaces (hook =
        # 'pre_tool_use'); associative-track surfaces (hook = 'pre_tool_use_assoc')
        # don't count — their tool call wasn't aimed at them.
        if not is_error:
            memory_ids = get_tool_call_surfaces(
                conn, session_id, tool_use_id, "pre_tool_use",
            )
            bump_useful_counts(conn, memory_ids)

        # Always increment the per-session turn counter (tracks conversational
        # distance for Hebbian co-activation). Runs regardless of error state —
        # every tool call is a turn.
        increment_session_turn(conn, session_id, now_ts)
    finally:
        conn.close()

    _emit({})
    return 0


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
