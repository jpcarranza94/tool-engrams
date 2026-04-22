"""PostToolUseFailure hook — hint injection on tool-call failure.

Fires only when a tool call has actually failed (Claude Code's PostToolUseFailure
event). See docs/design-v9.md §5 for why this event is the right surface moment
for hints: Claude Code already discriminates real failures from semantically-OK
non-zero exits (e.g. grep no-match), so we don't have to sniff exit codes.

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

Behavior (v2, design-v9 §3.3):
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


# Same whitelist as pretool — tools that carry user-facing memory bindings.
WHITELIST = {"Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "WebFetch", "NotebookEdit"}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"engram post-tool-failure: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover - fail-open safety net
        print(f"engram post-tool-failure: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any]) -> int:
    # User interrupted — not a real tool failure. No hints.
    if payload.get("is_interrupt"):
        _emit({})
        return 0

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

    candidates = retrieve_candidates(conn, hint, project_slug, now_ts, kind="hint")
    if not candidates:
        _emit({})
        return 0

    surfaced_ids = get_already_surfaced(conn, session_id)
    fresh = [c for c in candidates if c.memory_id not in surfaced_ids]
    if not fresh:
        _emit({})
        return 0

    fresh.sort(key=lambda c: (-len(c.matched_tokens), -c.final_score))

    memory_ids = [c.memory_id for c in fresh]
    current_turn = get_session_turn(conn, session_id)
    log_surfaces(conn, session_id, memory_ids, tool_use_id,
                 "post_tool_use_failure", current_turn, now_ts)
    bump_surface_counts(conn, memory_ids, now_ts)

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure",
            "additionalContext": format_injection(fresh),
        }
    })
    return 0


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
