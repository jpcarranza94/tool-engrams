"""PostToolUse hook command — turn counter + recovery fast-path tick.

This hook does not credit usefulness. Crediting a memory whenever a tool call
merely succeeds saturates useful_count — most tool calls succeed, so noise gets
reinforced. The single writer of `useful_count` / `noise_count` /
`session_surfaces.outcome` is the evaluation watcher (`engram judge`), which
reads the transcript and judges actual heeding.

What stays here:
  - `increment_session_turn` — the per-session tool-call counter that feeds
    `turn_at_surface` and `find_latest_active_session`.
  - the recovery fast-path tick — when a prior failure surface's first_token
    just succeeded, an error→fix episode is provably present and that surface's
    evidence window just closed. We fire a watcher tick now (don't credit)
    instead of waiting for the next Stop.

Output: {} (no injection).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .. import db
from ..retrieval.extract import extract_hints
from ..retrieval.session_state import (
    get_prior_failure_surfaces,
    increment_session_turn,
)
from ..utils import is_watcher_child
from ..watcher import derive_transcript_path, tick
from ._skip import is_internal_cwd


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
    # A watcher session's own tool calls must not count turns or fire ticks.
    if is_watcher_child():
        _emit({})
        return 0

    tool_use_id = payload.get("tool_use_id") or ""
    session_id = payload.get("session_id") or ""
    is_error = _detect_error(payload)

    if not tool_use_id or not session_id:
        _emit({})
        return 0

    now_ts = int(time.time())
    recovered = False  # a prior failure with this first_token just succeeded
    with db.session() as conn:
        if not is_error:
            # Detect — but do NOT credit — a prior failure surface whose
            # first_token matches this successful call. Crediting moved to the
            # eval watcher; here the failure→success pair only triggers an early
            # tick. Non-whitelisted tools yield empty hint.tokens, so the
            # `if first_token` short-circuits naturally.
            tool_name = payload.get("tool_name") or ""
            hint = extract_hints(tool_name, payload.get("tool_input") or {})
            first_token = hint.tokens[0] if hint.tokens else None
            if first_token and get_prior_failure_surfaces(conn, session_id, first_token):
                recovered = True

        increment_session_turn(conn, session_id, now_ts)

    # Fast-path trigger: fire watcher ticks now (outside the txn, so the Popen
    # never holds the DB) instead of waiting for the next Stop. A failure→success
    # recovery closes the failure-surface's evidence window, so it's an early
    # trigger for BOTH roles: formation (the error→fix episode just completed)
    # and eval (the prior failure surface can now be judged). trigger_eval
    # self-gates on pending surfaces, so it's a no-op when there's nothing to judge.
    if recovered and not is_watcher_child():
        cwd = payload.get("cwd") or ""
        if cwd and not is_internal_cwd(cwd):
            tpath = payload.get("transcript_path") or derive_transcript_path(session_id, cwd)
            tick.trigger(session_id, tpath, cwd, reason="recovery")
            tick.trigger_eval(session_id, tpath, cwd, reason="recovery")

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
