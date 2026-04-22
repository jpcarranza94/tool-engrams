"""PreToolUse hook command.

Reads a Claude Code PreToolUse payload from stdin, retrieves tool-bound memories
via subsequence matching on the call's tokens (plus path-glob on any paths),
ranks and filters them, logs the surface event, and emits an additionalContext
injection on stdout.

Note: this is still the v1-behavior pretool (feedback → deny, reference →
context). v2 step 2 rewrites it to block-only and moves hint injection to
PostToolUse. For step 1 we only update the data layer and matcher.

Contract (input JSON on stdin):
    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "/home/user/projects/myapp",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
      "tool_use_id": "..."
    }

Contract (output JSON on stdout):
    {
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "...",
        "permissionDecision": "allow"
      }
    }

Empty output (no matching memories) is `{}` — the harness treats this as a no-op.
Fails open: any exception logs to stderr and exits 0 with empty output so the
recall layer never blocks a tool call.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .. import db
from ..prompts.pretool import format_injection
from ..reinforcement.counters import bump_surface_counts
from ..retrieval.extract import extract_hints
from ..retrieval.rank import (
    compute_cluster_stats,
    filter_candidates,
    now,
    retrieve_candidates,
)
from ..retrieval.session_state import (
    get_already_surfaced,
    get_session_turn,
    log_surfaces,
)
from ..utils import slugify_cwd


# Tool whitelist — only these carry user-facing PreToolUse bindings.
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

    candidates = retrieve_candidates(conn, hint, project_slug, now_ts)
    if not candidates:
        _emit({})
        return 0

    cluster_stats = compute_cluster_stats(conn, project_slug, now_ts)
    surfaced_ids = get_already_surfaced(conn, session_id)
    primary = filter_candidates(candidates, cluster_stats, surfaced_ids)

    if not primary:
        _emit({})
        return 0

    current_turn = get_session_turn(conn, session_id)
    primary_ids = [c.memory_id for c in primary]
    log_surfaces(conn, session_id, primary_ids, tool_use_id,
                 "pre_tool_use", current_turn, now_ts)
    bump_surface_counts(conn, primary_ids, now_ts)

    additional_context = format_injection(primary)

    should_deny = any(c.type == "feedback" for c in primary)

    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": additional_context,
                "permissionDecision": "deny" if should_deny else "allow",
            }
        }
    )
    return 0


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
