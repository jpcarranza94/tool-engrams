"""Async observer: lightweight background candidate memory formation.

Spawned by PostToolUse as a detached background process. Sends a simple
Haiku prompt with recent context and the current tool call. If Haiku
thinks it's a candidate worth keeping, saves it via engram remember.

The consolidator (Opus, 6 PM) reviews these candidates with full context
and prunes the ones that aren't worth keeping. The observer is fast
triage, not thorough analysis.

Sessions spawned by the observer use a distinctive cwd prefix
(engram-observe-*) so the consolidator can skip them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import db
from .prompts.observer import OBSERVER_PROMPT
from .queries import get_existing_memories_summary
from .subprocess_utils import parse_claude_json_output
from .transcript import read_recent_context

CLAUDE_BIN = shutil.which("claude")
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path.home() / ".claude" / "tool-engrams" / "observer.log"

_SKIP_HEADS = {
    "ls", "echo", "cat", "head", "tail", "wc", "pwd", "which", "true",
    "false", "cd", "mkdir", "touch", "rm", "cp", "mv", "chmod",
    "engram",
}

_MIN_CMD_LENGTH = 20


def main(argv: list[str] | None = None) -> int:
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


def _log(msg: str) -> None:
    """Append a line to the observer log for monitoring."""
    try:
        import time
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _observe(payload: dict) -> int:
    if not CLAUDE_BIN:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""

    if tool_name != "Bash":
        return 0

    command = tool_input.get("command", "")
    if not command or len(command) < _MIN_CMD_LENGTH:
        return 0

    first_token = command.split()[0] if command.split() else ""
    if first_token in _SKIP_HEADS:
        return 0

    context = read_recent_context(transcript_path, session_id)
    if not context:
        return 0

    _log(f"OBSERVE cmd={first_token} len={len(command)}")

    existing = get_existing_memories_summary(db.connect())
    prompt = _build_prompt(command, context, existing)

    # Run from a temp dir with engram-observe- prefix so the consolidator
    # can identify and skip these sessions.
    work_dir = tempfile.mkdtemp(prefix="engram-observe-")

    env = os.environ.copy()
    env["ENGRAM_DB"] = os.environ.get(
        "ENGRAM_DB", str(Path.home() / ".claude" / "tool-engrams" / "db.sqlite")
    )
    env["PYTHONPATH"] = str(REPO_ROOT)

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", "haiku", "--output-format", "json", prompt],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, Exception):
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    response_text = parse_claude_json_output(proc.stdout)
    if not response_text:
        _log("SKIP no-response")
        return 0

    _log(f"RESULT {response_text[:100]}")
    _try_save_from_judgment(response_text)
    return 0


def _try_save_from_judgment(response_text: str) -> None:
    """Parse Haiku's JSON response and save if it's a candidate."""
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
    triggers = judgment.get("triggers", [])

    if not name or not body:
        return

    from .commands.remember import main as remember_main
    argv = [body, "--type", type_, "--scope", scope, "--name", name]
    for t in triggers:
        if isinstance(t, str) and t.strip():
            argv.extend(["--trigger", t.strip()])
    remember_main(argv)


def _build_prompt(command: str, context: str, existing: str) -> str:
    return f"""{OBSERVER_PROMPT}

## Recent Context

{context}

## Current Tool Call

```
{command[:500]}
```

## Existing Memories (don't duplicate these)

{existing}
"""
