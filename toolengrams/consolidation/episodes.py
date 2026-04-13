"""Episode extraction from JSONL transcripts.

Extracts two types of episodes:
  Type A (surfacing_eval): A memory was surfaced → what tool was called → did it succeed?
  Type B (correction): User corrected Claude's tool usage → candidate for new memory.

Episodes are small structured dicts, not full transcript lines. They're designed
to be compact enough to batch into a single LLM prompt (Phase 3) or process
mechanically (Phase 2).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .collect import SessionFile

# Truncation limits per field.
MAX_FIELD_CHARS = 500
MAX_SURFACING_EPISODES = 30
MAX_CORRECTION_EPISODES = 15

# Correction detection patterns.
_CORRECTION_RE = re.compile(
    r"\b(don't|dont|do not|never|always|instead|not that|wrong|should be|"
    r"use .+ instead|prefer .+ over)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class SurfacingEpisode:
    """A memory surfacing event + what happened next."""
    session_id: str
    memory_id: int
    memory_name: str
    tool_name: str | None
    tool_input_preview: str
    tool_succeeded: bool | None
    tool_result_preview: str


@dataclass(slots=True)
class CorrectionEpisode:
    """A user message that looks like a correction of tool usage."""
    session_id: str
    user_message: str
    preceding_tool: str | None
    preceding_tool_input: str


def extract_surfacing_episodes(
    conn: sqlite3.Connection,
    sessions: list[SessionFile],
    target_date_ts_range: tuple[int, int],
) -> list[SurfacingEpisode]:
    """Cross-reference session_surfaces with JSONL to build surfacing episodes."""
    start_ts, end_ts = target_date_ts_range

    # Get all surfaces from the target date.
    rows = conn.execute(
        "SELECT ss.session_id, ss.memory_id, ss.tool_use_id, m.name "
        "FROM session_surfaces ss "
        "JOIN memories m ON m.id = ss.memory_id "
        "WHERE ss.surfaced_ts BETWEEN ? AND ? "
        "AND ss.hook = 'pre_tool_use' "
        "ORDER BY ss.surfaced_ts",
        (start_ts, end_ts),
    ).fetchall()

    if not rows:
        return []

    # Build a quick lookup: session_id → JSONL path
    session_paths = {s.session_id: s.path for s in sessions}

    # Cache parsed tool calls per session
    session_tools: dict[str, list[dict]] = {}

    episodes: list[SurfacingEpisode] = []
    for row in rows:
        if len(episodes) >= MAX_SURFACING_EPISODES:
            break

        sid = row["session_id"]
        tool_use_id = row["tool_use_id"]

        if sid not in session_tools and sid in session_paths:
            session_tools[sid] = _extract_tool_calls(session_paths[sid])

        tool_call = _find_tool_call(session_tools.get(sid, []), tool_use_id)

        episodes.append(SurfacingEpisode(
            session_id=sid,
            memory_id=row["memory_id"],
            memory_name=row["name"],
            tool_name=tool_call.get("tool_name") if tool_call else None,
            tool_input_preview=_truncate(tool_call.get("input_preview", "")) if tool_call else "",
            tool_succeeded=tool_call.get("succeeded") if tool_call else None,
            tool_result_preview=_truncate(tool_call.get("result_preview", "")) if tool_call else "",
        ))

    return episodes


def extract_correction_episodes(
    sessions: list[SessionFile],
) -> list[CorrectionEpisode]:
    """Scan user messages for correction patterns."""
    episodes: list[CorrectionEpisode] = []

    for sf in sessions:
        if len(episodes) >= MAX_CORRECTION_EPISODES:
            break

        try:
            lines = _read_jsonl(sf.path)
        except Exception:
            continue

        preceding_tool = None
        preceding_input = ""

        for obj in lines:
            msg = obj.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Track the last tool call for context.
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        preceding_tool = block.get("name")
                        inp = block.get("input", {})
                        preceding_input = _truncate(
                            inp.get("command", "") or inp.get("file_path", "") or str(inp)[:200]
                        )

            # Check user messages for correction patterns.
            if role == "user" and isinstance(content, str):
                if _CORRECTION_RE.search(content):
                    episodes.append(CorrectionEpisode(
                        session_id=sf.session_id,
                        user_message=_truncate(content),
                        preceding_tool=preceding_tool,
                        preceding_tool_input=preceding_input,
                    ))
                    if len(episodes) >= MAX_CORRECTION_EPISODES:
                        break

    return episodes


# ---------- JSONL parsing helpers ----------


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL, skip malformed lines."""
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return results


def _extract_tool_calls(path: Path) -> list[dict]:
    """Extract tool call summaries from a JSONL transcript."""
    lines = _read_jsonl(path)
    calls: list[dict] = []
    pending: dict[str, dict] = {}  # tool_use_id → partial call

    for obj in lines:
        msg = obj.get("message", {})
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue

        for block in content:
            if block.get("type") == "tool_use":
                tool_id = block.get("id", "")
                inp = block.get("input", {})
                pending[tool_id] = {
                    "tool_use_id": tool_id,
                    "tool_name": block.get("name"),
                    "input_preview": inp.get("command", "") or inp.get("file_path", "") or str(inp)[:200],
                    "succeeded": None,
                    "result_preview": "",
                }
            elif block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    text_parts = [r.get("text", "") for r in result_content if isinstance(r, dict)]
                    result_text = " ".join(text_parts)
                else:
                    result_text = str(result_content)

                is_error = block.get("is_error", False)
                if not is_error and ("Exit code" in result_text[:50] or result_text.startswith("<error>")):
                    is_error = True

                if tool_id in pending:
                    pending[tool_id]["succeeded"] = not is_error
                    pending[tool_id]["result_preview"] = result_text[:MAX_FIELD_CHARS]
                    calls.append(pending.pop(tool_id))

    return calls


def _find_tool_call(calls: list[dict], tool_use_id: str | None) -> dict | None:
    if not tool_use_id or not calls:
        return None
    for c in calls:
        if c.get("tool_use_id") == tool_use_id:
            return c
    return None


def _truncate(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"
