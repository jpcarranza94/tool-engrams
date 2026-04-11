"""PostToolUse hook command — failure-recall subset.

Claude Code uses a single `PostToolUse` event for both successful and failed
tool calls. We only want to inject memory when the call failed, so this
handler inspects the payload for an error signal and no-ops otherwise.

Retrieval matches `error_contains` triggers (substring on error text) optionally
scoped to the tool + head. Recovery-hint memories like "ssh timeout → check VPN"
live here.

Input JSON on stdin (subset):
    {
      "session_id": "...",
      "cwd": "...",
      "hook_event_name": "PostToolUse",
      "tool_name": "Bash",
      "tool_input": {...},
      "tool_response": {
        "stdout": "...", "stderr": "...",
        "interrupted": false,
        "is_error": true          // or inferred from stderr/error_code
      },
      "tool_use_id": "..."
    }

Output:
    {
      "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "..."
      }
    }
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .. import db
from ..extract import extract_hints
from ..models import Candidate
from ..rank import final_score
from .pretool import slugify_cwd

MAX_INJECTION_CHARS = 4000
MAX_BODY_CHARS = 1000
TOP_K = 2

WHITELIST = {"Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob", "WebFetch", "NotebookEdit"}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"memctl post-failure: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover
        print(f"memctl post-failure: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any]) -> int:
    tool_name = payload.get("tool_name") or ""
    if tool_name not in WHITELIST:
        _emit({})
        return 0

    tool_response = payload.get("tool_response") or {}
    error_text = _extract_error_text(tool_response)
    if not error_text:
        _emit({})
        return 0

    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    tool_use_id = payload.get("tool_use_id")
    cwd = payload.get("cwd") or ""
    project_slug = slugify_cwd(cwd) if cwd else None

    hint = extract_hints(tool_name, tool_input)

    conn = db.connect()
    now_ts = int(time.time())

    candidates = _retrieve_error_matches(conn, tool_name, hint, error_text, project_slug, now_ts)
    if not candidates:
        _emit({})
        return 0

    surfaced_ids = _already_surfaced(conn, session_id)
    candidates = [c for c in candidates if c.memory_id not in surfaced_ids]
    if not candidates:
        _emit({})
        return 0

    candidates.sort(key=lambda c: -c.final_score)
    selected = candidates[:TOP_K]

    _log_surfaces(conn, session_id, selected, tool_use_id, now_ts)
    _bump_surface_counts(conn, selected, now_ts)

    context = _format_injection(selected)
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }
    )
    return 0


def _extract_error_text(tool_response: dict[str, Any]) -> str:
    """Return a string describing the error, or empty if the call succeeded."""
    if not tool_response:
        return ""
    if tool_response.get("is_error") is True:
        pass  # explicit failure signal
    elif tool_response.get("interrupted") is True:
        pass  # interrupted is a failure for our purposes
    else:
        # Fall back: if stderr has content and there's no stdout/stdout is empty,
        # treat it as a probable failure.
        stderr = (tool_response.get("stderr") or "").strip()
        stdout = (tool_response.get("stdout") or "").strip()
        error_field = (tool_response.get("error") or "").strip()
        if not error_field and not (stderr and not stdout):
            return ""

    parts = [
        tool_response.get("error"),
        tool_response.get("stderr"),
        tool_response.get("message"),
    ]
    return "\n".join(p for p in parts if p)


def _retrieve_error_matches(
    conn,
    tool_name: str,
    hint,
    error_text: str,
    project_slug: str | None,
    now_ts: int,
) -> list[Candidate]:
    candidates: dict[int, Candidate] = {}

    rows = conn.execute(
        """
        SELECT m.id, m.name, m.body, m.type, m.scope,
               m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
               t.tool_name, t.head_joined, t.head_length, t.error_substring
        FROM triggers t
        JOIN memories m ON m.id = t.memory_id
        WHERE t.kind = 'error_contains'
          AND t.tool_name = ?
          AND m.archived_ts IS NULL
          AND (m.scope = 'global' OR m.project_slug = ?)
        """,
        (tool_name, project_slug),
    ).fetchall()

    error_lower = error_text.lower()
    call_heads = {" ".join(h) for h in hint.head_prefixes}

    for row in rows:
        substring = (row["error_substring"] or "").lower()
        if not substring or substring not in error_lower:
            continue
        # If the memory also scopes to a head, require a head match.
        stored_head = row["head_joined"] or ""
        if stored_head:
            if not any(
                ch == stored_head or ch.startswith(stored_head + " ") or ch.startswith(stored_head)
                for ch in call_heads
            ):
                continue
        if row["id"] in candidates:
            continue
        candidates[row["id"]] = Candidate(
            memory_id=row["id"],
            name=row["name"],
            body=row["body"],
            tool_name=row["tool_name"],
            head_joined=row["head_joined"],
            head_length=row["head_length"] or 0,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            type=row["type"],
            scope=row["scope"],
            structural_match=1.2,  # small boost: error recall is high-signal
        )

    for c in candidates.values():
        c.final_score = final_score(c, now_ts)

    return list(candidates.values())


def _already_surfaced(conn, session_id: str) -> set[int]:
    if not session_id:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM session_surfaces WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["memory_id"] for r in rows}


def _log_surfaces(conn, session_id: str, selected, tool_use_id, now_ts) -> None:
    if not session_id:
        return
    rows = [
        (session_id, c.memory_id, now_ts, "post_tool_use_failure", tool_use_id)
        for c in selected
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _bump_surface_counts(conn, selected, now_ts) -> None:
    ids = [c.memory_id for c in selected]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE memories SET surface_count = surface_count + 1, "
        f"last_surfaced_ts = ? WHERE id IN ({placeholders})",
        (now_ts, *ids),
    )


def _format_injection(selected) -> str:
    parts: list[str] = []
    remaining = MAX_INJECTION_CHARS
    for c in selected:
        body = c.body.strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
        block = f"[recovery memory: {c.name}]\n{body}"
        if len(block) + 2 > remaining:
            break
        parts.append(block)
        remaining -= len(block) + 2
    header = "Recovery memories for this failure:\n\n"
    return header + "\n\n".join(parts) if parts else ""


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
