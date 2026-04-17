"""Async observer: lightweight background candidate memory formation.

Spawned by PostToolUse as a detached background process. Sends a simple
Haiku prompt with recent context and the current tool call. If Haiku
thinks it's a candidate worth keeping, saves it via engram remember.

The consolidator (Opus, 8 AM) reviews these candidates with full context
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
from .transcript import is_sidechain_call, read_recent_context

CLAUDE_BIN = shutil.which("claude")
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path.home() / ".claude" / "tool-engrams" / "observer.log"

# JSON schema for constrained decoding. The observer must return either a
# "skip" object or a "save" object with memory details. --json-schema
# guarantees the output matches at the token level — no parsing fallbacks.
OBSERVER_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["skip", "save"],
        },
        "name": {"type": "string"},
        "body": {"type": "string"},
        "type": {
            "type": "string",
            "enum": ["feedback", "reference"],
        },
        "scope": {
            "type": "string",
            "enum": ["project", "global"],
        },
        "triggers": {
            "type": "array",
            "items": {"type": "string"},
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["action"],
})

_SKIP_HEADS = {
    "ls", "echo", "cat", "head", "tail", "wc", "pwd", "which", "true",
    "false", "cd", "mkdir", "touch", "rm", "cp", "mv", "chmod",
    "engram",
}

_MIN_CMD_LENGTH = 20

# File tools (Edit/Write/MultiEdit) give us path-bound signal — Claude is
# modifying code in a specific area. Edit/Write carry clearer intent than
# Read, so we only observe mutations.
_FILE_TOOLS = {"Edit", "Write", "MultiEdit"}

# Path substrings that indicate generated/dependency code not worth memory
# formation. Keep the list small — we want to be lenient here.
_PATH_NOISE = (
    "node_modules/", ".venv/", "venv/", "__pycache__/",
    ".git/", "dist/", "build/", ".next/", "target/",
)


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
    cwd = payload.get("cwd") or ""

    # Extract the signal depending on the tool kind. Bash gives us a
    # command; Edit/Write/MultiEdit give us a file path.
    signal_kind, signal_value = _extract_signal(tool_name, tool_input)
    if signal_kind is None:
        return 0

    # Skip tool calls originating from Task-tool-spawned sidechains.
    if is_sidechain_call(payload):
        _log(f"SKIP sidechain tool={tool_name} agent_type={payload.get('agent_type')}")
        return 0

    context = read_recent_context(transcript_path, session_id)
    if not context:
        return 0

    _log(f"OBSERVE tool={tool_name} {signal_kind}={signal_value[:60]}")

    existing = get_existing_memories_summary(db.connect())
    prompt = _build_prompt(tool_name, signal_kind, signal_value, context, existing, cwd)

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
            [
                CLAUDE_BIN, "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", OBSERVER_SCHEMA,
                prompt,
            ],
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
    _try_save_from_judgment(response_text, cwd)
    return 0


def _try_save_from_judgment(response_text: str, cwd: str = "") -> None:
    """Parse the observer's JSON response and save if it's a candidate.

    With --json-schema, the response is guaranteed to be valid JSON matching
    OBSERVER_SCHEMA. The try/except is kept as a safety net for edge cases
    (timeouts, empty responses, CLI version mismatch).
    """
    try:
        judgment = json.loads(response_text.strip())
    except (json.JSONDecodeError, ValueError):
        return

    if judgment.get("action") != "save":
        return

    name = judgment.get("name", "")
    body = judgment.get("body", "")
    type_ = judgment.get("type", "reference")
    scope = judgment.get("scope", "project")
    triggers = judgment.get("triggers", [])
    paths = judgment.get("paths", [])

    if not name or not body:
        return

    # Need at least one trigger or path for the memory to fire on anything.
    if not triggers and not paths:
        return

    # Set ENGRAM_PROJECT_CWD so remember.py resolves the correct project slug
    # (the observer runs from a temp dir, not the original session's cwd).
    if cwd:
        os.environ["ENGRAM_PROJECT_CWD"] = cwd

    from .commands.remember import main as remember_main
    argv = [body, "--type", type_, "--scope", scope, "--name", name]
    for t in triggers:
        if isinstance(t, str) and t.strip():
            argv.extend(["--trigger", t.strip()])
    for p in paths:
        if isinstance(p, str) and p.strip():
            argv.extend(["--path", p.strip()])
    remember_main(argv)


def _extract_signal(
    tool_name: str, tool_input: dict
) -> tuple[str | None, str]:
    """Extract the observer's signal from a tool call.

    Returns (kind, value) where kind is "command" (Bash) or "file" (Edit/
    Write/MultiEdit), or (None, "") to skip. Applies per-tool gating.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        if not command or len(command) < _MIN_CMD_LENGTH:
            return None, ""
        first_token = command.split()[0] if command.split() else ""
        if first_token in _SKIP_HEADS:
            return None, ""
        return "command", command

    if tool_name in _FILE_TOOLS:
        file_path = tool_input.get("file_path", "") or ""
        if not file_path:
            return None, ""
        # Skip generated/dependency paths.
        if any(noise in file_path for noise in _PATH_NOISE):
            return None, ""
        return "file", file_path

    return None, ""


def _build_prompt(
    tool_name: str,
    signal_kind: str,
    signal_value: str,
    context: str,
    existing: str,
    cwd: str = "",
) -> str:
    cwd_section = f"\n## Project Directory\n\n{cwd}\n" if cwd else ""
    if signal_kind == "command":
        call_section = f"## Current Tool Call (Bash)\n\n```\n{signal_value[:500]}\n```"
    else:  # file
        call_section = (
            f"## Current Tool Call ({tool_name})\n\n"
            f"File: `{signal_value}`\n\n"
            "If this file's area has knowledge Claude would need "
            "when working elsewhere on similar files, consider a "
            "path_glob memory via --path (e.g. `**/tax.py`, "
            "`**/migrations/*.py`)."
        )
    return f"""{OBSERVER_PROMPT}
{cwd_section}
## Recent Context

{context}

{call_section}

## Existing Memories (don't duplicate these)

{existing}
"""
