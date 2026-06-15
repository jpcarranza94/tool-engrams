"""Pure JSONL → readable-conversation conversion for the watcher.

No DB, no subprocess. Given a list of JSONL lines from a Claude Code
transcript, produce a single string the watcher model can read. Long deltas
are tail-trimmed so dormant sessions don't blow the model's context budget.
"""

from __future__ import annotations

import json

from ..transcript_utils import (
    MAX_BASH_CMD_CHARS,
    MAX_RESULT_CHARS,
    _cap_delta,
    _clip_ends,
    _clip_head,
)

# JSONL line types to skip during delta formatting.
_SKIP_TYPES = {"queue-operation", "attachment", "last-prompt"}


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
                    parts.append(f'AGENT: "{text[:300]}"')
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
                            parts.append(f'AGENT: "{text[:300]}"')

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
