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
from ..retrieval.extract import extract_hints
from ..retrieval.session_state import (
    get_prior_failure_surfaces,
    get_tool_call_surfaces,
    increment_session_turn,
    mark_surface_outcome,
)
from ._skip import WHITELIST


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
    with db.session() as conn:
        if not is_error:
            # (1) Pre-tool surfaces from this exact call: bump useful_count
            #     and mark the surface row 'helpful'.
            pre_ids = get_tool_call_surfaces(
                conn, session_id, tool_use_id, "pre_tool_use",
            )
            if pre_ids:
                bump_useful_counts(conn, pre_ids)
                mark_surface_outcome(
                    conn, session_id, pre_ids, "helpful",
                    hook="pre_tool_use",
                )

            # (2) Prior failure surfaces in this session with matching
            #     first_token: same-shape call now succeeded, so credit the
            #     hint. Only whitelisted tools (others have no useful
            #     first_token extraction).
            tool_name = payload.get("tool_name") or ""
            if tool_name in WHITELIST:
                hint = extract_hints(tool_name, payload.get("tool_input") or {})
                first_token = hint.tokens[0] if hint.tokens else None
                if first_token:
                    failure_ids = get_prior_failure_surfaces(
                        conn, session_id, first_token,
                    )
                    if failure_ids:
                        bump_useful_counts(conn, failure_ids)
                        mark_surface_outcome(
                            conn, session_id, failure_ids, "helpful",
                            hook="post_tool_use_failure",
                            first_token=first_token,
                        )

        increment_session_turn(conn, session_id, now_ts)

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
