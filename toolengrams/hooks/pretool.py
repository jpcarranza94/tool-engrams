"""PreToolUse hook — blocks + hints.

Surfaces BOTH kinds of memory before every whitelisted tool call:
  - block memories -> permissionDecision: deny (Claude retries with context)
  - hint memories  -> additionalContext only, NO permissionDecision

If ANY block matches, the entire call is denied (block takes precedence).
If only hints match, the context is injected and the permission flow is left
untouched — an explicit "allow" here would silently bypass the user's
permission prompts for any command an autonomously-formed hint trigger
happens to match, which is an escalation a memory must never grant.

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

from .. import db, memory_store, pause
from ..prompts.pretool import format_injection
from ..retrieval.extract import extract_hints
from ..reinforcement.scoring import is_gated
from ..retrieval.rank import now, retrieve_candidates
from ..retrieval.session_state import (
    HOOK_PRE_TOOL_USE,
    get_already_surfaced,
    get_session_turn,
    log_surfaces,
)
from ..utils import is_watcher_child, slugify_cwd
from ._skip import WHITELIST, max_memories_per_call, surface_notice


def main() -> int:
    if pause.is_disabled():
        _emit({})
        return 0
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
    # A watcher session's own `engram` tool calls must not surface memories
    # (recursion + polluting the watcher's claude session). The internal-cwd
    # guard backs this up; the env flag is the robust primary check.
    if is_watcher_child():
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

    with db.session() as conn:
        now_ts = now()

        # Retrieve ALL matching memories (both block and hint).
        candidates = retrieve_candidates(conn, hint, project_slug)
        if not candidates:
            _emit({})
            return 0

        # Surfacing gate: suppress hints that have proven more noise than signal
        # (q < 0.5 after warm-up). block + pinned are exempt (see scoring.is_gated).
        candidates = [c for c in candidates if not is_gated(c)]

        # Same-session suppression (ADR-0006): a hint never surfaces into the
        # session that formed it — the session already lived the episode, and a
        # same-session "helpful" would be self-confirmation, not transfer.
        # Blocks are exempt: enforcement must fire where the lesson was learned.
        candidates = [c for c in candidates
                      if c.kind == "block" or not c.origin_session_id
                      or c.origin_session_id != session_id]
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
                     HOOK_PRE_TOOL_USE, current_turn, now_ts,
                     first_token=first_token)
        memory_store.bump_surface(conn, memory_ids, now_ts)

    # Deny if ANY block matches. Hints carry NO permissionDecision: an
    # explicit "allow" would bypass the user's permission prompts (hint
    # triggers form autonomously — they must never grant approval).
    has_block = any(c.kind == "block" for c in fresh)

    hso: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "additionalContext": format_injection(fresh),
    }
    if has_block:
        hso["permissionDecision"] = "deny"
    out: dict[str, Any] = {"hookSpecificOutput": hso}
    notice = surface_notice([c.name for c in fresh])
    if notice:
        out["systemMessage"] = notice
    _emit(out)
    return 0


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
