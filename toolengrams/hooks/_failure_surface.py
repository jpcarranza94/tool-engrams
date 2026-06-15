"""Shared failure-moment hint surfacing."""

from __future__ import annotations

from typing import Any

from .. import db, memory_store
from ..prompts.pretool import format_injection
from ..reinforcement.scoring import is_gated
from ..retrieval.rank import now, retrieve_candidates
from ..retrieval.session_state import (
    HOOK_POST_TOOL_USE_FAILURE,
    get_already_surfaced,
    get_session_turn,
    log_surfaces,
)
from ..utils import is_watcher_child, slugify_cwd
from ..watcher import tick
from ._skip import max_memories_per_call, surface_notice


def surface_failure_hints(
    payload: dict[str, Any],
    target,
    *,
    output_event_name: str,
) -> dict[str, Any]:
    """Return hook JSON for hints that match a failed tool call.

    The stored surface hook stays `post_tool_use_failure` even for targets
    without a dedicated failure event; `output_event_name` names the hook
    response schema currently being written.
    """
    if is_watcher_child() or payload.get("is_interrupt"):
        return {}

    sid = payload.get("session_id") or ""
    if sid:
        tick.arm(sid, target.transcript_path(payload), payload.get("cwd") or "",
                 target=target.NAME)

    tool_name = payload.get("tool_name") or ""
    if tool_name not in target.tool_whitelist:
        return {}

    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    tool_use_id = payload.get("tool_use_id")
    cwd = payload.get("cwd") or ""
    project_slug = slugify_cwd(cwd) if cwd else None

    hint = target.extract_hints(tool_name, tool_input)
    if not hint.tokens and not hint.paths:
        return {}

    with db.session() as conn:
        now_ts = now()
        candidates = retrieve_candidates(conn, hint, project_slug, kind="hint")
        candidates = [c for c in candidates if not is_gated(c)]
        candidates = [c for c in candidates
                      if not c.origin_session_id or c.origin_session_id != session_id]
        if not candidates:
            return {}

        surfaced_ids = get_already_surfaced(conn, session_id)
        fresh = [c for c in candidates if c.memory_id not in surfaced_ids]
        if not fresh:
            return {}

        fresh.sort(key=lambda c: (-len(c.matched_tokens), -c.final_score))
        fresh = fresh[: max_memories_per_call()]
        memory_ids = [c.memory_id for c in fresh]
        first_token = hint.tokens[0] if hint.tokens else None
        log_surfaces(conn, session_id, memory_ids, tool_use_id,
                     HOOK_POST_TOOL_USE_FAILURE, get_session_turn(conn, session_id),
                     now_ts, first_token=first_token)
        memory_store.bump_surface(conn, memory_ids, now_ts)

    out: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": output_event_name,
            "additionalContext": format_injection(fresh),
        }
    }
    notice = surface_notice([c.name for c in fresh])
    if notice:
        out["systemMessage"] = notice
    return out
