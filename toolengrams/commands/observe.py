"""Async observer: background memory formation from tool-call context.

Spawned by PostToolUse as a background process. Reads the session's JSONL
transcript for recent context, asks Haiku if there's a pattern worth
remembering, and runs engram remember if yes.

This runs OUTSIDE the hook pipeline — it's a fire-and-forget background
process that never blocks the user's session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import db
from ..prompts.observer import OBSERVER_SYSTEM

CLAUDE_BIN = shutil.which("claude")

# Only observe Bash calls with nontrivial commands.
# Simple commands (ls, echo, cat, head, tail, wc) are not worth observing.
_SKIP_HEADS = {
    "ls", "echo", "cat", "head", "tail", "wc", "pwd", "which", "true",
    "false", "cd", "mkdir", "touch", "rm", "cp", "mv", "chmod",
    "engram",  # don't observe our own tool
}

# Minimum command length worth observing.
_MIN_CMD_LENGTH = 20

# Max recent JSONL lines to scan for context.
_MAX_CONTEXT_LINES = 200

# Max tool call excerpts to include (most recent).
_MAX_TOOL_EXCERPTS = 10

# Max user message excerpts to include (ALL from session, truncated).
_MAX_USER_MSG_CHARS = 300


def main(argv: list[str] | None = None) -> int:
    """Entry point — reads the PostToolUse payload from argv or stdin."""
    if argv and len(argv) >= 1:
        try:
            payload = json.loads(argv[0])
        except (json.JSONDecodeError, IndexError):
            return 0
    else:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 0

    return _observe(payload)


def _observe(payload: dict) -> int:
    if not CLAUDE_BIN:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""

    # Gate: only observe Bash with nontrivial commands.
    if tool_name != "Bash":
        return 0

    command = tool_input.get("command", "")
    if not command or len(command) < _MIN_CMD_LENGTH:
        return 0

    first_token = command.split()[0] if command.split() else ""
    if first_token in _SKIP_HEADS:
        return 0

    # Get recent context from the session transcript.
    context = _read_recent_context(transcript_path, session_id)
    if not context:
        return 0

    # Get existing memories for dedup context.
    existing = _get_existing_memories()

    # Build prompt.
    prompt = _build_prompt(command, context, existing)

    # Call Haiku in background — fire and forget.
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", "haiku", "--output-format", "json", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, Exception):
        return 0

    # Parse response.
    response_text = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                outer = json.loads(line)
                response_text = outer.get("result", "")
                break
            except json.JSONDecodeError:
                continue

    if not response_text:
        return 0

    # Parse Haiku's judgment.
    try:
        # Strip markdown fences if present.
        clean = response_text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(l for l in lines if not l.startswith("```")).strip()

        judgment = json.loads(clean)
    except json.JSONDecodeError:
        # Try finding JSON in the response.
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start < 0 or end <= start:
            return 0
        try:
            judgment = json.loads(response_text[start:end])
        except json.JSONDecodeError:
            return 0

    if judgment.get("action") == "skip":
        return 0

    # Has a memory to save.
    name = judgment.get("name", "")
    body = judgment.get("body", "")
    type_ = judgment.get("type", "reference")
    scope = judgment.get("scope", "global")

    if not name or not body or "`" not in body:
        return 0

    # Run engram remember (gets dedup + triggerless gate for free).
    from .remember import main as remember_main
    remember_main([body, "--type", type_, "--scope", scope, "--name", name])
    return 0


def _read_recent_context(transcript_path: str, session_id: str) -> str:
    """Extract ALL user prompts + recent tool calls from the session.

    User prompts carry intent and corrections — they're the most valuable
    signal for deciding if a tool pattern is worth remembering. We include
    every user message from the entire session, plus the most recent tool
    calls for immediate context.
    """
    path = _find_transcript(transcript_path, session_id)
    if not path:
        return ""

    try:
        with open(path) as f:
            lines = f.readlines()
    except Exception:
        return ""

    user_messages: list[str] = []
    tool_calls: list[str] = []

    for line in lines[-_MAX_CONTEXT_LINES:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = obj.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Collect ALL user messages — they carry corrections and intent.
        if role == "user" and isinstance(content, str):
            text = content.strip()
            if text and not text.startswith("<"):  # skip system tags
                user_messages.append(f"USER: {text[:_MAX_USER_MSG_CHARS]}")

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

    # All user messages + last N tool calls.
    parts = []
    if user_messages:
        parts.append("=== User messages (full session) ===")
        parts.extend(user_messages)
    if tool_calls:
        parts.append(f"\n=== Recent tool calls (last {_MAX_TOOL_EXCERPTS}) ===")
        parts.extend(tool_calls[-_MAX_TOOL_EXCERPTS * 2:])  # *2 for TOOL+RESULT pairs

    return "\n".join(parts)


def _find_transcript(transcript_path: str, session_id: str) -> Path | None:
    """Locate the session JSONL file."""
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


def _get_existing_memories() -> str:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT name, body FROM memories WHERE archived_ts IS NULL ORDER BY id"
        ).fetchall()
        if not rows:
            return "No existing memories."
        return "\n".join(f"- {r['name']}: {r['body'][:100]}" for r in rows)
    finally:
        conn.close()


def _build_prompt(command: str, context: str, existing: str) -> str:
    return f"""{OBSERVER_SYSTEM}

## Recent Context

{context}

## Current Tool Call

```
{command[:500]}
```

## Existing Memories (don't duplicate)

{existing}
"""
