"""SessionStart hook command.

Injects formation guidance: tells Claude how and when to use `engram remember`
to form tool-bound memories. This is the only job of SessionStart — all
memory surfacing happens via PreToolUse when the matching tool call fires.

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


def main() -> int:
    try:
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _formation_guidance(),
            }
        })
        return 0
    except Exception as e:  # pragma: no cover
        print(f"engram session-start: unexpected error: {e}", file=sys.stderr)
        _emit({})
        return 0


def _formation_guidance() -> str:
    return (
        "[ToolEngrams: tool-bound memory]\n"
        "You have ToolEngrams — a memory system for facts bound to specific tool calls. "
        "Memories surface automatically via PreToolUse when you call matching tools.\n\n"
        "ONLY save things that are about how to use a specific command or file:\n"
        "  Run: engram remember \"<body>\" --type <feedback|reference> "
        "--scope <global|project> [--name \"<short name>\"]\n\n"
        "The body MUST include backticked commands (e.g. `git push`, `mycli -c`) or file "
        "paths. Triggers are extracted from these patterns — a body without them is rejected.\n\n"
        "When to save (tool-bound facts only):\n"
        "- User corrects how to use a command → type=feedback\n"
        "- User confirms a non-obvious tool usage → type=feedback\n"
        "- You learn how a specific CLI/tool/file should be used → type=reference\n\n"
        "Do NOT save: user preferences, project deadlines, team info, or anything without "
        "a tool-call binding. Those belong in Claude's built-in memory system, not here.\n\n"
        "To FORGET: engram forget \"<name>\"  |  To BROWSE: engram recall [query]"
    )


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
