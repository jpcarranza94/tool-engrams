"""PreToolUse hook — blocks + hints.

Surfaces BOTH kinds of memory before every whitelisted tool call:
  - block memories -> permissionDecision: deny (Claude retries with context)
  - hint memories  -> permissionDecision: allow + additionalContext

If ANY block matches, the entire call is denied (block takes precedence).
If only hints match, the call proceeds with injected context.

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
from ._skip import WHITELIST, max_memories_per_call


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

    with db.session() as conn:
        now_ts = now()

        # Retrieve ALL matching memories (both block and hint).
        candidates = retrieve_candidates(conn, hint, project_slug, now_ts)
        if not candidates:
            _emit({})
            return 0

        # Session dedup — don't re-show the same memory twice in a session.
        surfaced_ids = get_already_surfaced(conn, session_id)
        fresh = [c for c in candidates if c.memory_id not in surfaced_ids]
        if not fresh:
            _emit({})
            return 0

        # Sort: longer triggers (more specific) win, then higher final_score.
        fresh.sort(key=lambda c: (-len(c.matched_tokens), -c.final_score))

        # Cap surfaces per call. Always keep matched blocks so the deny path
        # can't be diluted; trim hints first.
        cap = max_memories_per_call()
        if len(fresh) > cap:
            blocks = [c for c in fresh if c.kind == "block"]
            hints = [c for c in fresh if c.kind == "hint"]
            remaining = max(cap - len(blocks), 0)
            fresh = blocks + hints[:remaining]

        memory_ids = [c.memory_id for c in fresh]
        current_turn = get_session_turn(conn, session_id)
        first_token = hint.tokens[0] if hint.tokens else None
        log_surfaces(conn, session_id, memory_ids, tool_use_id,
                     "pre_tool_use", current_turn, now_ts,
                     first_token=first_token)
        bump_surface_counts(conn, memory_ids, now_ts)

    # Deny if ANY block matches; allow (with context) if only hints.
    has_block = any(c.kind == "block" for c in fresh)

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": format_injection(fresh),
            "permissionDecision": "deny" if has_block else "allow",
        }
    })
    return 0


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
