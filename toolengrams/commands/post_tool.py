"""PostToolUse hook command — success reinforcement + async observer.

Two jobs:
  1. (Sync) Bump useful_count for memories that were surfaced on this tool call.
  2. (Async) Spawn a background observer that reads recent context and decides
     if there's a new tool-usage pattern worth remembering.

Output: {} (no injection — both jobs are silent).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import db

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = sys.executable


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

    # Reinforcement: only on success.
    if not is_error:
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT memory_id FROM session_surfaces "
                "WHERE session_id = ? AND tool_use_id = ? AND hook = 'pre_tool_use'",
                (session_id, tool_use_id),
            ).fetchall()
            if rows:
                memory_ids = [r["memory_id"] for r in rows]
                placeholders = ",".join("?" * len(memory_ids))
                conn.execute(
                    f"UPDATE memories SET useful_count = useful_count + 1 "
                    f"WHERE id IN ({placeholders})",
                    memory_ids,
                )
        finally:
            conn.close()

    # Async observer: spawn background process to analyze this tool call.
    _spawn_observer(payload)

    _emit({})
    return 0


def _spawn_observer(payload: dict[str, Any]) -> None:
    """Fire-and-forget: spawn engram observe as a background process."""
    try:
        payload_json = json.dumps(payload)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        subprocess.Popen(
            [PYTHON_BIN, "-m", "toolengrams", "observe", payload_json],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # fully detach from parent
        )
    except Exception:
        pass  # observer is best-effort — never block the hook


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
