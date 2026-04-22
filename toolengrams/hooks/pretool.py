"""PreToolUse hook — block-only.

v2 (design-v9 §3.1): only `block`-kind memories surface in PreToolUse. Every
match produces `permissionDecision: deny` + the memory body as
`additionalContext`. Hint-kind memories live on a separate track wired into
PostToolUseFailure (step 3).

A deny here doesn't "fail the call for the user" — it fails the call *for
Claude* and prompts a retry with the injected context in scope. See §1a.

Contract (input):
    {
      "session_id": "...",
      "cwd": "/home/user/projects/myapp",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "git push --force origin main"},
      "tool_use_id": "..."
    }

Contract (output):
    {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "...",
        "permissionDecision": "deny"
    }}

Empty output (`{}`) on no match. Fails open: any exception logs to stderr
and exits 0 with empty output so the hook never blocks a tool call.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .. import db
from ..prompts.pretool import format_injection
from ..reinforcement.counters import bump_surface_counts
from ..retrieval.extract import extract_hints
from ..retrieval.rank import now, retrieve_candidates
from ..retrieval.session_state import (
    get_already_surfaced,
    get_session_turn,
    log_surfaces,
)
from ..utils import slugify_cwd


WHITELIST = {"Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "WebFetch", "NotebookEdit"}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"engram pretool: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover - fail-open safety net
        print(f"engram pretool: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any]) -> int:
    tool_name = payload.get("tool_name") or ""
    if tool_name not in WHITELIST:
        _emit({})
        return 0

    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    tool_use_id = payload.get("tool_use_id")
    cwd = payload.get("cwd") or ""
    project_slug = slugify_cwd(cwd) if cwd else None

    hint = extract_hints(tool_name, tool_input)
    if not hint.tokens and not hint.paths:
        _emit({})
        return 0

    conn = db.connect()
    now_ts = now()

    # Blocks only. Hints don't surface at PreToolUse — they go out on failure.
    candidates = retrieve_candidates(conn, hint, project_slug, now_ts, kind="block")
    if not candidates:
        _emit({})
        return 0

    # Session dedup — the same block already surfaced this session doesn't re-fire.
    surfaced_ids = get_already_surfaced(conn, session_id)
    fresh = [c for c in candidates if c.memory_id not in surfaced_ids]
    if not fresh:
        _emit({})
        return 0

    # Sort: longer triggers (more specific) win, then higher final_score.
    fresh.sort(key=lambda c: (-len(c.matched_tokens), -c.final_score))

    memory_ids = [c.memory_id for c in fresh]
    current_turn = get_session_turn(conn, session_id)
    log_surfaces(conn, session_id, memory_ids, tool_use_id,
                 "pre_tool_use", current_turn, now_ts)
    bump_surface_counts(conn, memory_ids, now_ts)

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": format_injection(fresh),
            "permissionDecision": "deny",
        }
    })
    return 0


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
