"""Persistent parallel watcher: background Haiku session for memory formation.

Spawned by SessionStart as a detached background process. Wakes every 5 minutes,
reads the JSONL transcript delta, and sends it to a persistent Haiku session for
evaluation. If Haiku identifies patterns worth remembering, saves them via
`engram remember`.

The watcher replaces the per-call observer with a single long-running session
that accumulates conversational context across the entire work session.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import db
from .cli.remember import main as remember_main
from .prompts.watcher import WATCHER_SUBSEQUENT_HEADER, build_watcher_prompt
from .subprocess_utils import parse_claude_json_output
from .utils import slugify_cwd

CLAUDE_BIN = shutil.which("claude")
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path.home() / ".claude" / "tool-engrams" / "watcher.log"
PYTHON_BIN = sys.executable

WATCHER_INTERVAL = 300  # 5 minutes
SESSION_TIMEOUT = 30  # minutes of inactivity before exit

# JSON schema for constrained decoding.
WATCHER_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["none", "create"],
        },
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "body": {"type": "string"},
                    "kind": {"type": "string", "enum": ["block", "hint"]},
                    "scope": {"type": "string", "enum": ["project", "global"]},
                    "triggers": {"type": "array", "items": {"type": "string"}},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "body", "kind", "scope"],
            },
        },
    },
    "required": ["action"],
})

# JSONL line types to skip during delta formatting.
_SKIP_TYPES = {"queue-operation", "attachment", "last-prompt"}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `engram watcher`."""
    if not argv or len(argv) < 3:
        print("Usage: engram watcher <session_id> <transcript_path> <cwd>", file=sys.stderr)
        return 1
    session_id, transcript_path, cwd = argv[0], argv[1], argv[2]
    return watcher_main(session_id, transcript_path, cwd)


def watcher_main(session_id: str, transcript_path: str, cwd: str) -> int:
    """Long-running cron: wake every 5 min, read delta, call Haiku."""
    _log(f"START session={session_id} transcript={transcript_path}")

    # Handle SIGTERM gracefully.
    def _handle_sigterm(signum, frame):
        _log(f"SIGTERM session={session_id}")
        _cleanup(session_id)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    watcher_session_id = None
    last_line = 0

    try:
        while True:
            time.sleep(WATCHER_INTERVAL)

            # Liveness: exit if transcript hasn't been touched in 30 min.
            if not _is_session_alive(transcript_path):
                _log(f"TIMEOUT session={session_id} (no activity for {SESSION_TIMEOUT} min)")
                break

            # Read new lines.
            new_lines = _read_lines_from(transcript_path, last_line)
            if not new_lines:
                _update_state(session_id, watcher_session_id, last_line)
                continue

            delta = _format_delta(new_lines)
            if not delta.strip():
                last_line += len(new_lines)
                _update_state(session_id, watcher_session_id, last_line)
                continue

            if not CLAUDE_BIN:
                last_line += len(new_lines)
                continue

            # Call Haiku.
            try:
                if watcher_session_id is None:
                    message = _build_initial_prompt(cwd) + delta
                    stdout = _claude_p_new(message, WATCHER_SCHEMA)
                    watcher_session_id = _extract_session_id(stdout)
                else:
                    message = WATCHER_SUBSEQUENT_HEADER + delta
                    stdout = _claude_p_resume(watcher_session_id, message, WATCHER_SCHEMA)
            except Exception as e:
                _log(f"HAIKU-ERROR session={session_id} error={e}")
                last_line += len(new_lines)
                _update_state(session_id, watcher_session_id, last_line)
                continue

            # Parse + save.
            response = _parse_response(stdout)
            action = (response or {}).get("action") or "parse_error"
            if action == "create":
                for mem in response.get("memories", []):
                    try:
                        _save_memory(mem, cwd)
                        _log(f"SAVE session={session_id} name={mem.get('name', '?')}")
                    except Exception as e:
                        _log(f"SAVE-ERROR session={session_id} error={e}")
            else:
                # Healthy-but-quiet: Haiku returned none / parse failed. Log so
                # we can distinguish "watcher ticking, model sees nothing to save"
                # from "watcher dead".
                _log(f"HAIKU-{action.upper()} session={session_id} lines={len(new_lines)}")

            last_line += len(new_lines)
            _update_state(session_id, watcher_session_id, last_line)
    except Exception as e:
        _log(f"CRASH session={session_id} error={e}")
    finally:
        _cleanup(session_id)

    _log(f"EXIT session={session_id}")
    return 0


def spawn_watcher(session_id: str, transcript_path: str, cwd: str) -> None:
    """Spawn watcher as a detached background process and record state."""
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)

        proc = subprocess.Popen(
            [PYTHON_BIN, "-m", "toolengrams", "watcher",
             session_id, transcript_path, cwd],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        now_ts = int(time.time())
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO watcher_state "
                "(work_session_id, watcher_pid, transcript_path, "
                " last_line_read, last_checked_ts, cwd, created_ts) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (session_id, proc.pid, transcript_path, now_ts, cwd, now_ts),
            )
        finally:
            conn.close()

        _log(f"SPAWN session={session_id} pid={proc.pid}")
    except Exception as e:
        _log(f"SPAWN-ERROR session={session_id} error={e}")


def derive_transcript_path(session_id: str, cwd: str) -> str:
    """Derive the JSONL transcript path from session_id and cwd."""
    slug = slugify_cwd(cwd)
    return str(Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl")


# ---------- internal ----------


def _build_initial_prompt(cwd: str) -> str:
    return f"{build_watcher_prompt()}\n\nProject: {cwd}\n\n--- Session activity ---\n\n"


def _is_session_alive(transcript_path: str, timeout_minutes: int = SESSION_TIMEOUT) -> bool:
    """Check if the transcript file has been modified recently."""
    try:
        mtime = Path(transcript_path).stat().st_mtime
        return (time.time() - mtime) < (timeout_minutes * 60)
    except (FileNotFoundError, OSError):
        return False


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lines_from(path: str, start_line: int) -> list[str]:
    """Read JSONL lines from start_line to EOF."""
    try:
        with open(path) as f:
            lines = f.readlines()
        return lines[start_line:]
    except (FileNotFoundError, OSError):
        return []


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

                    # Preserve error messages in full.
                    is_error = block.get("is_error", False)
                    if is_error or "ERROR" in result_text[:100].upper():
                        parts.append(f"RESULT: {result_text}")
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
                            parts.append(f"TOOL (Bash): {cmd}")
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

    return "\n".join(parts)


def _claude_p_new(message: str, schema: str) -> str:
    """Start a new Haiku session. Returns stdout.

    Uses --bare to skip hooks — prevents the watcher's Haiku session
    from triggering SessionStart which would spawn another watcher
    (recursive fork bomb).
    """
    proc = subprocess.run(
        [
            CLAUDE_BIN, "-p",
            "--bare",
            "--model", "haiku",
            "--output-format", "json",
            "--json-schema", schema,
            message,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout


def _claude_p_resume(session_id: str, message: str, schema: str) -> str:
    """Resume an existing Haiku session. Returns stdout.

    Uses --bare to skip hooks (see _claude_p_new docstring).
    """
    proc = subprocess.run(
        [
            CLAUDE_BIN, "-p",
            "--bare",
            "--model", "haiku",
            "--output-format", "json",
            "--json-schema", schema,
            "--resume", session_id,
            message,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout


def _extract_session_id(stdout: str) -> str | None:
    """Extract session_id from claude -p --output-format json output."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                sid = payload.get("session_id")
                if sid:
                    return sid
            except json.JSONDecodeError:
                continue
    return None


def _parse_response(stdout: str) -> dict | None:
    """Extract and parse the JSON response from claude -p output."""
    result_text = parse_claude_json_output(stdout)
    if not result_text:
        return None
    try:
        return json.loads(result_text.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _legacy_type_to_kind(type_value: str | None) -> str:
    if type_value == "feedback":
        return "block"
    return "hint"


def _save_memory(mem: dict, cwd: str) -> None:
    """Save a memory by calling engram remember."""
    name = mem.get("name", "")
    body = mem.get("body", "")
    # Accept both new schema field ("kind") and legacy ("type") from older
    # watcher sessions mid-transition; map legacy values to new ones.
    kind = mem.get("kind") or _legacy_type_to_kind(mem.get("type"))
    scope = mem.get("scope", "project")
    triggers = mem.get("triggers", [])
    paths = mem.get("paths", [])

    if not name or not body:
        return
    if not triggers and not paths:
        return

    if cwd:
        os.environ["ENGRAM_PROJECT_CWD"] = cwd

    argv = [body, "--kind", kind, "--scope", scope, "--name", name]
    for t in triggers:
        if isinstance(t, str) and t.strip():
            argv.extend(["--trigger", t.strip()])
    for p in paths:
        if isinstance(p, str) and p.strip():
            argv.extend(["--path", p.strip()])
    remember_main(argv)


def _update_state(
    session_id: str,
    watcher_session_id: str | None,
    last_line: int,
) -> None:
    """Update watcher_state table with current progress."""
    try:
        now_ts = int(time.time())
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE watcher_state SET "
                "watcher_session_id = ?, last_line_read = ?, last_checked_ts = ? "
                "WHERE work_session_id = ?",
                (watcher_session_id, last_line, now_ts, session_id),
            )
        finally:
            conn.close()
    except Exception:
        pass


def _cleanup(session_id: str) -> None:
    """Remove watcher_state row on exit."""
    try:
        conn = db.connect()
        try:
            conn.execute(
                "DELETE FROM watcher_state WHERE work_session_id = ?",
                (session_id,),
            )
        finally:
            conn.close()
    except Exception:
        pass


def _log(msg: str) -> None:
    """Append a line to the watcher log."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
