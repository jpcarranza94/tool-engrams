"""UserPromptSubmit hook command.

Retrieve memories relevant to the user's prompt text via keyword triggers,
path references, and FTS match. Apply the per-cluster Laplace threshold
with the same reinforcement scoring as PreToolUse.

Input JSON on stdin (subset):
    {
      "session_id": "...",
      "cwd": "...",
      "hook_event_name": "UserPromptSubmit",
      "prompt": "the user's text"
    }

Output:
    {
      "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "..."
      }
    }
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys
import time
from typing import Any

from .. import db
from ..models import Candidate
from ..rank import (
    FilterConfig,
    filter_candidates,
    final_score,
)
from .pretool import slugify_cwd

MAX_INJECTION_CHARS = 6000
MAX_BODY_CHARS = 1200
TOP_K = 3


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"engram user-prompt: invalid JSON on stdin: {e}", file=sys.stderr)
        _emit({})
        return 0

    try:
        return _run(payload)
    except Exception as e:  # pragma: no cover
        print(f"engram user-prompt: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _run(payload: dict[str, Any]) -> int:
    prompt_text = (payload.get("prompt") or "").strip()
    if not prompt_text:
        _emit({})
        return 0

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    project_slug = slugify_cwd(cwd) if cwd else None

    conn = db.connect()
    now_ts = int(time.time())

    candidates = _retrieve_for_prompt(conn, prompt_text, project_slug, now_ts)
    if not candidates:
        _emit({})
        return 0

    surfaced_ids = _already_surfaced(conn, session_id)
    cfg = FilterConfig(top_k=TOP_K)
    selected = filter_candidates(candidates, cluster_stats={}, surfaced_ids=surfaced_ids, cfg=cfg)

    if not selected:
        _emit({})
        return 0

    _log_surfaces(conn, session_id, selected, now_ts)
    _bump_surface_counts(conn, selected, now_ts)

    context = _format_injection(selected)
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
    )
    return 0


def _retrieve_for_prompt(
    conn,
    prompt_text: str,
    project_slug: str | None,
    now_ts: int,
) -> list[Candidate]:
    """Keyword substring + FTS5 match + keyword-trigger match."""
    candidates: dict[int, Candidate] = {}
    prompt_lower = prompt_text.lower()

    # --- keyword trigger match (substring of prompt) ---
    keyword_rows = conn.execute(
        """
        SELECT m.id, m.name, m.body, m.type, m.scope,
               m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
               t.keyword
        FROM triggers t
        JOIN memories m ON m.id = t.memory_id
        WHERE t.kind = 'keyword'
          AND m.archived_ts IS NULL
          AND (m.scope = 'global' OR m.project_slug = ?)
        """,
        (project_slug,),
    ).fetchall()
    for row in keyword_rows:
        kw = (row["keyword"] or "").lower()
        if kw and kw in prompt_lower:
            _add_candidate(candidates, row)

    # --- path_glob trigger match (prompt text mentions a path) ---
    path_mentions = _extract_paths_from_text(prompt_text)
    if path_mentions:
        path_rows = conn.execute(
            """
            SELECT m.id, m.name, m.body, m.type, m.scope,
                   m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
                   t.path_pattern
            FROM triggers t
            JOIN memories m ON m.id = t.memory_id
            WHERE t.kind = 'path_glob'
              AND m.archived_ts IS NULL
              AND (m.scope = 'global' OR m.project_slug = ?)
            """,
            (project_slug,),
        ).fetchall()
        for row in path_rows:
            pat = row["path_pattern"]
            if pat and any(fnmatch.fnmatchcase(p, pat) for p in path_mentions):
                _add_candidate(candidates, row)

    # --- FTS match on memory body ---
    fts_query = _build_fts_query(prompt_text)
    if fts_query:
        try:
            fts_rows = conn.execute(
                """
                SELECT m.id, m.name, m.body, m.type, m.scope,
                       m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned
                FROM memories_fts f
                JOIN memories m ON m.id = f.rowid
                WHERE memories_fts MATCH ?
                  AND m.archived_ts IS NULL
                  AND (m.scope = 'global' OR m.project_slug = ?)
                LIMIT 20
                """,
                (fts_query, project_slug),
            ).fetchall()
            for row in fts_rows:
                _add_candidate(candidates, row)
        except Exception as e:
            print(f"engram user-prompt: FTS query failed: {e}", file=sys.stderr)

    for c in candidates.values():
        c.final_score = final_score(c, now_ts)

    return list(candidates.values())


_PATH_RE = re.compile(r"(?<!\S)(~(?:/[^\s;|&><]*)?|/[^\s;|&><]+)")
_WORD_RE = re.compile(r"\w{3,}")


def _extract_paths_from_text(text: str) -> list[str]:
    return [m for m in _PATH_RE.findall(text)]


def _build_fts_query(prompt_text: str) -> str | None:
    """Turn the prompt into a safe FTS5 query.

    FTS5 is picky about special characters; we extract word tokens, dedupe,
    and OR them together. Skip if there are no usable tokens.
    """
    words = {w.lower() for w in _WORD_RE.findall(prompt_text)}
    # Drop very common English stopwords that blow up recall.
    stop = {
        "the", "and", "for", "you", "are", "with", "this", "that", "from",
        "what", "when", "where", "how", "why", "can", "will", "should",
        "have", "has", "had", "but", "not", "any", "all", "get", "got",
        "your", "been", "was", "were", "were", "its", "it's", "do", "does",
        "did", "done", "make", "made", "one", "two", "some", "more",
    }
    tokens = [w for w in words if w not in stop and len(w) >= 3]
    if not tokens:
        return None
    tokens = tokens[:12]
    # Quote each token to defang FTS5 syntax chars, join with OR.
    return " OR ".join(f'"{t}"' for t in tokens)


def _add_candidate(store: dict[int, Candidate], row) -> None:
    if row["id"] in store:
        return
    store[row["id"]] = Candidate(
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
        structural_match=1.0,
    )


def _already_surfaced(conn, session_id: str) -> set[int]:
    if not session_id:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM session_surfaces WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["memory_id"] for r in rows}


def _log_surfaces(conn, session_id: str, selected: list[Candidate], now_ts: int) -> None:
    if not session_id:
        return
    rows = [(session_id, c.memory_id, now_ts, "user_prompt_submit", None) for c in selected]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _bump_surface_counts(conn, selected: list[Candidate], now_ts: int) -> None:
    ids = [c.memory_id for c in selected]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE memories SET surface_count = surface_count + 1, "
        f"last_surfaced_ts = ? WHERE id IN ({placeholders})",
        (now_ts, *ids),
    )


def _format_injection(selected: list[Candidate]) -> str:
    parts: list[str] = []
    remaining = MAX_INJECTION_CHARS
    for c in selected:
        body = c.body.strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[: MAX_BODY_CHARS - 1].rstrip() + "…"
        block = f"[memory: {c.name}]\n{body}"
        if len(block) + 2 > remaining:
            break
        parts.append(block)
        remaining -= len(block) + 2
    header = "Relevant memories for your request:\n\n"
    return header + "\n\n".join(parts) if parts else ""


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
