"""SessionStart hook command.

Eager-inject the identity layer: `type: user` memories + any pinned memories,
scoped to the current project (plus globals). Reinforcement-exempt — these
memories are by definition always relevant.

Input JSON on stdin (subset):
    {
      "session_id": "...",
      "cwd": "...",
      "hook_event_name": "SessionStart",
      "source": "startup" | "resume" | "clear" | "compact"
    }

Output:
    {
      "hookSpecificOutput": {
        "hookEventName": "SessionStart",
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
from .pretool import slugify_cwd

MAX_INJECTION_CHARS = 8000
MAX_BODY_CHARS = 1500
MAX_MEMORIES = 10


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"engram session-start: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover
        print(f"engram session-start: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any]) -> int:
    cwd = payload.get("cwd") or ""
    project_slug = slugify_cwd(cwd) if cwd else None
    session_id = payload.get("session_id") or ""

    conn = db.connect()
    rows = conn.execute(
        """
        SELECT id, name, body, type, scope, pinned
        FROM memories
        WHERE archived_ts IS NULL
          AND (type = 'user' OR pinned = 1)
          AND (scope = 'global' OR project_slug = ?)
        ORDER BY pinned DESC, id ASC
        LIMIT ?
        """,
        (project_slug, MAX_MEMORIES),
    ).fetchall()

    if not rows:
        _emit({})
        return 0

    now_ts = int(time.time())
    _log_surfaces(conn, session_id, rows, now_ts)

    context = _format_injection(rows)
    if not context:
        _emit({})
        return 0

    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
    )
    return 0


def _format_injection(rows) -> str:
    parts: list[str] = []
    remaining = MAX_INJECTION_CHARS
    for row in rows:
        body = (row["body"] or "").strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
        tag = "pinned" if row["pinned"] else row["type"]
        block = f"[memory: {row['name']} ({tag})]\n{body}"
        if len(block) + 2 > remaining:
            break
        parts.append(block)
        remaining -= len(block) + 2
    header = "Session memories:\n\n"
    return header + "\n\n".join(parts) if parts else ""


def _log_surfaces(conn, session_id: str, rows, now_ts: int) -> None:
    if not session_id:
        return
    entries = [
        (session_id, row["id"], now_ts, "session_start", None) for row in rows
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id) "
        "VALUES (?, ?, ?, ?, ?)",
        entries,
    )
    conn.execute(
        "UPDATE memories SET surface_count = surface_count + 1, last_surfaced_ts = ? "
        "WHERE id IN ({})".format(",".join(str(r["id"]) for r in rows)),
        (now_ts,),
    )


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
