"""PreToolUse hook command.

Reads a Claude Code PreToolUse payload from stdin, retrieves tool-bound memories,
ranks and filters them, logs the surface event, and emits an additionalContext
injection on stdout.

Contract (input JSON on stdin):
    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "/home/user/projects/myapp",
      "hook_event_name": "PreToolUse",
      "tool_name": "Bash",
      "tool_input": {"command": "mycli -c 'SELECT 1'"},
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
from pathlib import Path
from typing import Any

from .. import db
from ..extract import extract_hints
from ..models import Candidate
from ..rank import (
    compute_cluster_stats,
    filter_candidates,
    now,
    retrieve_candidates,
)

# Cap the injected context to stay well under the 10k-char hook limit.
MAX_INJECTION_CHARS = 6000
MAX_BODY_CHARS = 1200

# Tool whitelist — only these carry user-facing PreToolUse bindings.
WHITELIST = {"Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "WebFetch", "NotebookEdit"}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"memctl pretool: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover - fail-open safety net
        print(f"memctl pretool: unexpected error: {e}", file=sys.stderr)
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

    cluster_stats = compute_cluster_stats(conn, project_slug, now_ts)
    surfaced_ids = _already_surfaced_this_session(conn, session_id)
    selected = filter_candidates(candidates, cluster_stats, surfaced_ids)

    if not selected:
        _emit({})
        return 0

    _log_surfaces(conn, session_id, selected, tool_use_id, now_ts)
    _bump_surface_counts(conn, selected, now_ts)

    additional_context = _format_injection(selected)
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": additional_context,
                "permissionDecision": "allow",
            }
        }
    )
    return 0


# ---------- helpers ----------


def slugify_cwd(cwd: str) -> str:
    """Match Claude Code's project-slug convention: `/` → `-`."""
    return cwd.replace("/", "-")


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
    now_ts: int,
) -> None:
    if not session_id:
        return
    rows = [
        (session_id, c.memory_id, now_ts, "pre_tool_use", tool_use_id)
        for c in candidates
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id) "
        "VALUES (?, ?, ?, ?, ?)",
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


def _format_injection(candidates: list[Candidate]) -> str:
    parts: list[str] = []
    remaining = MAX_INJECTION_CHARS
    for c in candidates:
        body = c.body.strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
        block = f"[memory: {c.name}]\n{body}"
        if len(block) + 2 > remaining:
            break
        parts.append(block)
        remaining -= len(block) + 2
    header = "Relevant memories for this tool call:\n\n"
    return header + "\n\n".join(parts) if parts else ""


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
