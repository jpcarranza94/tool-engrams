"""PreToolUse hook command.

Reads a Claude Code PreToolUse payload from stdin, retrieves tool-bound memories,
ranks and filters them, logs the surface event, and emits an additionalContext
injection on stdout.

Two retrieval tracks run side-by-side:

  * Primary: structurally-matched memories (the tool-head / path-glob hit this
    tool call). These pass the Laplace-smoothed threshold and drive the
    deny/allow decision.
  * Associative: memories linked to what already surfaced earlier in this
    session via Hebbian co-fire. Injected as a separate section and never deny.

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
from ..associations import (
    get_prior_surfaces_with_turn,
    get_session_turn,
    record_co_activations,
    retrieve_associates_of,
)
from ..extract import extract_hints
from ..models import Candidate
from ..prompts.pretool import format_injection
from ..utils import slugify_cwd
from ..rank import (
    compute_cluster_stats,
    filter_candidates,
    now,
    retrieve_candidates,
)


# Tool whitelist — only these carry user-facing PreToolUse bindings.
WHITELIST = {"Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "WebFetch", "NotebookEdit"}

# Max associative memories surfaced per call. Primary top-K is in rank.TOP_K.
N_ASSOC = 2

# Minimum effective association boost to qualify for the associative pool.
# ASSOC_BOOST (0.3) * strength=0.167 ≈ 0.05 — roughly "co-fired a few times".
MIN_ASSOC_BOOST = 0.05


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
    if not hint.head_prefixes and not hint.paths:
        _emit({})
        return 0

    conn = db.connect()
    now_ts = now()

    candidates = retrieve_candidates(conn, hint, project_slug, now_ts)
    if not candidates:
        _emit({})
        return 0

    # Hebbian state (before logging this call's surfaces).
    prior_surfaces_turn = get_prior_surfaces_with_turn(conn, session_id)
    prior_surfaced_ids = set(prior_surfaces_turn.keys())
    current_turn = get_session_turn(conn, session_id)

    # --- Primary phase: structural match → filter → top-K ---
    cluster_stats = compute_cluster_stats(conn, project_slug, now_ts)
    surfaced_ids = _already_surfaced_this_session(conn, session_id)
    primary = filter_candidates(candidates, cluster_stats, surfaced_ids)
    primary_ids = {c.memory_id for c in primary}

    # --- Associative phase: Hebbian links to prior surfaces ---
    # Unlike primary, associative memories don't need to structurally match the
    # current tool call — they're surfaced because they're linked to something
    # already in play this session.
    exclude = primary_ids | surfaced_ids
    assoc_pairs = retrieve_associates_of(
        conn,
        prior_surfaced_ids=prior_surfaced_ids,
        exclude_ids=exclude,
        project_slug=project_slug,
        now_ts=now_ts,
        min_boost=MIN_ASSOC_BOOST,
    )
    associative = _hydrate_associates(conn, assoc_pairs[:N_ASSOC])

    if not primary and not associative:
        _emit({})
        return 0

    # Log surfaces for BOTH tracks; associative rows use a distinct hook tag so
    # reinforcement (which targets 'pre_tool_use' only) skips them.
    if primary:
        _log_surfaces(conn, session_id, primary, tool_use_id, "pre_tool_use",
                      current_turn, now_ts)
        _bump_surface_counts(conn, primary, now_ts)
    if associative:
        _log_surfaces(conn, session_id, associative, tool_use_id,
                      "pre_tool_use_assoc", current_turn, now_ts)
        _bump_surface_counts(conn, associative, now_ts)

    # Hebbian: record co-activations AFTER logging. Both tracks count as
    # newly-surfaced for co-fire purposes — they're all live this turn.
    newly_surfaced_ids = [c.memory_id for c in primary] + [c.memory_id for c in associative]
    record_co_activations(
        conn, session_id,
        newly_surfaced_ids=newly_surfaced_ids,
        prior_surfaced=prior_surfaces_turn,
        current_turn=current_turn,
        now_ts=now_ts,
    )

    additional_context = format_injection(primary, associative)

    # Deny decision: PRIMARY only. Associative memories are context, never block.
    should_deny = any(
        c.type == "feedback" and c.head_joined is not None
        for c in primary
    )

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


# ---------- helpers ----------


def _hydrate_associates(
    conn, pairs: list[tuple[int, float]],
) -> list[Candidate]:
    """Load Candidate objects for associative memories (by id), preserving boost order."""
    if not pairs:
        return []
    ids = [mid for mid, _ in pairs]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, name, body, type, scope, surface_count, useful_count, "
        f"last_surfaced_ts, pinned FROM memories WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    result: list[Candidate] = []
    for mid, boost in pairs:
        row = by_id.get(mid)
        if row is None:
            continue
        result.append(Candidate(
            memory_id=row["id"],
            name=row["name"],
            body=row["body"],
            tool_name=None,
            head_joined=None,
            head_length=0,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            type=row["type"],
            scope=row["scope"],
            association_boost=boost,
        ))
    return result


def _already_surfaced_this_session(conn, session_id: str) -> set[int]:
    if not session_id:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM session_surfaces WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["memory_id"] for r in rows}


def _log_surfaces(
    conn,
    session_id: str,
    candidates: list[Candidate],
    tool_use_id: str | None,
    hook: str,
    turn_at_surface: int,
    now_ts: int,
) -> None:
    if not session_id:
        return
    rows = [
        (session_id, c.memory_id, now_ts, hook, tool_use_id, turn_at_surface)
        for c in candidates
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _bump_surface_counts(conn, candidates: list[Candidate], now_ts: int) -> None:
    ids = [c.memory_id for c in candidates]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE memories SET surface_count = surface_count + 1, "
        f"last_surfaced_ts = ? WHERE id IN ({placeholders})",
        (now_ts, *ids),
    )



def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
