"""Session transcript (JSONL) reading and parsing."""

from __future__ import annotations

import json
from pathlib import Path

# Max JSONL lines to scan.
MAX_CONTEXT_LINES = 200
MAX_TOOL_EXCERPTS = 10
MAX_USER_MSG_CHARS = 300


def find_transcript(transcript_path: str, session_id: str) -> Path | None:
    """Locate a session's JSONL file by path or session_id search."""
    if transcript_path and Path(transcript_path).exists():
        return Path(transcript_path)

    projects = Path.home() / ".claude" / "projects"
    if projects.is_dir():
        for d in projects.iterdir():
            if d.is_dir():
                candidate = d / f"{session_id}.jsonl"
                if candidate.exists():
                    return candidate
    return None


def is_sidechain_call(payload: dict) -> bool:
    """Check whether this tool call originated inside a Task-spawned subagent.

    Claude Code's PreToolUse/PostToolUse hook payload includes `agent_id`
    and `agent_type` fields ONLY when the tool call was made by a
    sidechain subagent spawned via the Task tool. Normal user-driven
    calls and agent-team subagent calls (different mechanism) do not
    include these fields.

    Rationale: Task sidechains are autonomous exploration/research that
    Claude spawned mid-conversation — not part of the user's actual
    workflow — so we skip memory formation on them.
    """
    return bool(payload.get("agent_id") or payload.get("agent_type"))


def read_recent_context(transcript_path: str, session_id: str) -> str:
    """Extract ALL user prompts + recent tool calls from a session.

    User prompts carry intent and corrections — the most valuable signal
    for deciding if a tool pattern is worth remembering. We include every
    user message, plus the most recent tool calls for immediate context.
    """
    path = find_transcript(transcript_path, session_id)
    if not path:
        return ""

    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        return ""

    user_messages: list[str] = []
    tool_calls: list[str] = []

    for line in lines[-MAX_CONTEXT_LINES:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = obj.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Collect ALL user messages.
        if role == "user" and isinstance(content, str):
            text = content.strip()
            if text and not text.startswith("<"):
                user_messages.append(f"USER: {text[:MAX_USER_MSG_CHARS]}")

        # Collect recent tool calls.
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    cmd = block.get("input", {}).get("command", "")
                    if cmd:
                        tool_calls.append(f"TOOL: {cmd[:300]}")
                elif block.get("type") == "tool_result":
                    rc = block.get("content", "")
                    text = rc if isinstance(rc, str) else str(rc)
                    if text and len(text) > 5:
                        tool_calls.append(f"RESULT: {text[:200]}")

    parts = []
    if user_messages:
        parts.append("=== User messages (full session) ===")
        parts.extend(user_messages)
    if tool_calls:
        parts.append(f"\n=== Recent tool calls (last {MAX_TOOL_EXCERPTS}) ===")
        parts.extend(tool_calls[-MAX_TOOL_EXCERPTS * 2:])

    return "\n".join(parts)
