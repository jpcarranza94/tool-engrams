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

import tempfile

from .. import db
from ..prompts.observer import OBSERVER_SYSTEM
from ..queries import get_existing_memories_summary
from ..subprocess_utils import parse_claude_json_output, write_agent_settings
from ..transcript import find_transcript, read_recent_context

CLAUDE_BIN = shutil.which("claude")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Only observe Bash calls with nontrivial commands.
# Simple commands (ls, echo, cat, head, tail, wc) are not worth observing.
_SKIP_HEADS = {
    "ls", "echo", "cat", "head", "tail", "wc", "pwd", "which", "true",
    "false", "cd", "mkdir", "touch", "rm", "cp", "mv", "chmod",
    "engram",  # don't observe our own tool
}

# Minimum command length worth observing.
_MIN_CMD_LENGTH = 20


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
    transcript_file = find_transcript(transcript_path, session_id)
    context = read_recent_context(transcript_path, session_id)
    if not context:
        return 0

    # Get existing memories for dedup context.
    existing = get_existing_memories_summary(db.connect())

    # Build prompt with file path so the agent can explore.
    prompt = _build_prompt(command, context, existing, transcript_file)

    # Set up a temp working dir with permissions for the agent.
    work_dir = tempfile.mkdtemp(prefix="engram-observe-")
    work_path = Path(work_dir)
    write_agent_settings(work_path, ["Read", "Grep", "Bash(engram *)"])

    env = os.environ.copy()
    env["ENGRAM_DB"] = os.environ.get("ENGRAM_DB", str(Path.home() / ".claude" / "tool-engrams" / "db.sqlite"))
    env["PYTHONPATH"] = str(REPO_ROOT)

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", "haiku", "--output-format", "json", prompt],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, Exception):
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # The agent may have already run engram remember directly.
    # Also check if it returned a JSON judgment in its response.
    response_text = parse_claude_json_output(proc.stdout)
    if not response_text:
        return 0

    # Try to parse a JSON judgment from the response (if the agent
    # returned one instead of calling engram remember directly).
    _try_save_from_judgment(response_text)
    return 0




def _try_save_from_judgment(response_text: str) -> None:
    """If the agent returned JSON instead of calling engram, save it."""
    try:
        clean = response_text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(l for l in lines if not l.startswith("```")).strip()
        judgment = json.loads(clean)
    except json.JSONDecodeError:
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start < 0 or end <= start:
            return
        try:
            judgment = json.loads(response_text[start:end])
        except json.JSONDecodeError:
            return

    if judgment.get("action") == "skip":
        return

    name = judgment.get("name", "")
    body = judgment.get("body", "")
    type_ = judgment.get("type", "reference")
    scope = judgment.get("scope", "global")

    if not name or not body or "`" not in body:
        return

    from .remember import main as remember_main
    remember_main([body, "--type", type_, "--scope", scope, "--name", name])



def _build_prompt(command: str, context: str, existing: str,
                   transcript_file: Path | None = None) -> str:
    transcript_section = ""
    if transcript_file:
        transcript_section = f"""
## Session Transcript (for deeper investigation)

File: {transcript_file}
Use Read or Grep on this file if the excerpt suggests something worth investigating further.
"""

    return f"""{OBSERVER_SYSTEM}

## Recent Context (excerpt)

{context}

## Current Tool Call

```
{command[:500]}
```

## Existing Memories (don't duplicate)

{existing}
{transcript_section}"""
