"""Pure JSONL → readable-conversation conversion for the watcher.

No DB, no subprocess. Given a list of JSONL lines from a Claude Code
transcript, produce a single string the watcher model can read. Long deltas
are tail-trimmed so dormant sessions don't blow the model's context budget.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# JSONL line types to skip during delta formatting.
_SKIP_TYPES = {"queue-operation", "attachment", "last-prompt"}

# Cap the formatted delta sent to the model. Dormant sessions can accumulate
# huge transcripts (one observed at 345 KB = ~86K tokens on a single call).
# Long deltas both cost more and dilute the signal — the model starts
# narrating the whole conversation rather than spotting extractable patterns.
# Keep the tail since recent activity is most likely to contain extractable
# patterns (errors + corrections that happened this interval).
MAX_DELTA_CHARS = 40_000

# Per-line caps. The overall MAX_DELTA_CHARS budget isn't enough on its own: a
# single tool call can be enormous (a `gh pr create` / `git commit` heredoc
# carrying a multi-KB PR or commit body), and a single full error dump can run
# to thousands of lines. One such line eats the whole budget and pushes the
# model call past its timeout — exactly what stalled the watcher on busy
# multi-PR sessions. The signal the watcher needs lives in the head of a
# command (the binary + flags) and at both ends of an error (the command that
# failed + the cause), not in a PR body. Cap each accordingly.
MAX_BASH_CMD_CHARS = 800
MAX_RESULT_CHARS = 1_000


def _clip_head(text: str, limit: int) -> str:
    """Keep the first `limit` chars, flagging how much was dropped."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[+{len(text) - limit} chars truncated]"


def _clip_ends(text: str, limit: int) -> str:
    """Keep head + tail. Errors usually lead with the failing command and end
    with the actual cause, so preserve both ends and elide the middle."""
    if len(text) <= limit:
        return text
    # The elision marker itself is ~12 chars; for a tiny limit a head+tail split
    # would inflate the result past the input, so just hard-truncate the head.
    if limit < 24:
        return text[: max(limit, 0)]
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}…[+{len(text) - limit} chars]…{text[-tail:]}"


def _read_lines_from(path: str, start_line: int) -> list[str]:
    """Read JSONL lines from start_line to EOF."""
    try:
        with open(path) as f:
            lines = f.readlines()
        return lines[start_line:]
    except (FileNotFoundError, OSError):
        return []


DEFAULT_SESSION_TIMEOUT_MIN = 30


def _is_session_alive(
    transcript_path: str,
    timeout_minutes: int = DEFAULT_SESSION_TIMEOUT_MIN,
) -> bool:
    """Check if the transcript file has been modified recently."""
    try:
        mtime = Path(transcript_path).stat().st_mtime
        return (time.time() - mtime) < (timeout_minutes * 60)
    except (FileNotFoundError, OSError):
        return False


def _format_delta(lines: list[str]) -> str:
    """Convert JSONL lines to human-readable conversation format.

    Skip: queue-operation, attachment, last-prompt, system-reminder content.
    Include: user messages, assistant text, tool_use, tool_result.
    """
    parts: list[str] = []

    for raw_line in lines:
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue

        obj_type = obj.get("type", "")

        # Skip known noise types.
        if obj_type in _SKIP_TYPES:
            continue

        msg = obj.get("message", {})
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

        # Skip system-reminder content.
        if isinstance(content, str) and "system-reminder" in content:
            continue

        # User messages.
        if role == "user" and isinstance(content, str):
            text = content.strip()
            if text and not text.startswith("<"):
                parts.append(f'USER: "{text[:500]}"')
            continue

        # User messages with list content (e.g. tool_result blocks).
        if role == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        # Extract text from content blocks.
                        texts = []
                        for item in result_content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                texts.append(item.get("text", ""))
                        result_text = "\n".join(texts)
                    elif isinstance(result_content, str):
                        result_text = result_content
                    else:
                        result_text = str(result_content)

                    if not result_text:
                        continue

                    # Preserve error messages (head + tail), trim successes.
                    is_error = block.get("is_error", False)
                    if is_error or "ERROR" in result_text[:100].upper():
                        parts.append(f"RESULT: {_clip_ends(result_text, MAX_RESULT_CHARS)}")
                    else:
                        parts.append(f"RESULT: {result_text[:200]}")
            continue

        # Assistant messages.
        if role == "assistant":
            if isinstance(content, str):
                text = content.strip()
                if text and "system-reminder" not in text:
                    parts.append(f'CLAUDE: "{text[:300]}"')
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    # Skip system-reminder blocks.
                    if isinstance(block.get("content"), str) and "system-reminder" in block["content"]:
                        continue

                    block_type = block.get("type", "")

                    if block_type == "text":
                        text = block.get("text", "").strip()
                        if text and "system-reminder" not in text:
                            parts.append(f'CLAUDE: "{text[:300]}"')

                    elif block_type == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})

                        if tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            parts.append(f"TOOL (Bash): {_clip_head(cmd, MAX_BASH_CMD_CHARS)}")
                        elif tool_name in ("Edit", "Write", "MultiEdit"):
                            file_path = tool_input.get("file_path", "")
                            parts.append(f"TOOL ({tool_name}): {file_path}")
                        elif tool_name in ("Read", "Glob", "Grep"):
                            # Include search/read tools with key info.
                            if tool_name == "Grep":
                                pattern = tool_input.get("pattern", "")
                                parts.append(f"TOOL (Grep): {pattern}")
                            elif tool_name == "Read":
                                file_path = tool_input.get("file_path", "")
                                parts.append(f"TOOL (Read): {file_path}")
                            elif tool_name == "Glob":
                                pattern = tool_input.get("pattern", "")
                                parts.append(f"TOOL (Glob): {pattern}")
                        else:
                            parts.append(f"TOOL ({tool_name})")
            continue

    joined = "\n".join(parts)
    return _cap_delta(joined)


def _cap_delta(text: str) -> str:
    if len(text) <= MAX_DELTA_CHARS:
        return text
    tail = text[-MAX_DELTA_CHARS:]
    # Don't start the tail mid-line — trim to the first newline.
    nl = tail.find("\n")
    if 0 <= nl < 2000:
        tail = tail[nl + 1 :]
    dropped = len(text) - len(tail)
    return f"[…earlier activity truncated — {dropped} chars / {dropped // 80} lines dropped…]\n{tail}"
