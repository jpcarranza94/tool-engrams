"""Watcher's `claude -p` agent calls + JSON response parsing.

The watcher invokes a `claude -p` session every interval, feeding it a
formatted transcript delta and a JSON schema for constrained decoding. The
model returns either `{"action": "none"}` or `{"action": "create",
"memories": [...]}`. We parse that, then save each memory by calling
`engram remember` in-process.

Model selection is via `$ENGRAM_WATCHER_MODEL` (default: opus).
"""

from __future__ import annotations

import json
import os
import re
import shutil

from ..claude_invoke import invoke_claude_agent, parse_claude_json_output
from ..cli.remember import main as remember_main
from ..prompts.watcher import build_watcher_prompt

CLAUDE_BIN = shutil.which("claude")

DEFAULT_WATCHER_MODEL = "opus"

# Per-call wall-clock budget for the watcher's `claude -p`. The original 60s
# was too tight: on a busy window the delta is large and opus is slow, so the
# call timed out and the window was dropped. Now the tick HOLDS the cursor and
# retries on error (see tick._retry_decision); 120s gives headroom. Tune via
# $ENGRAM_WATCHER_TIMEOUT without restarting.
DEFAULT_WATCHER_TIMEOUT = 120

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


def _watcher_model() -> str:
    """Resolve the model name for the watcher's `claude -p` calls.

    Reads $ENGRAM_WATCHER_MODEL each time so tests and per-shell overrides
    take effect without restarting the process. Default is opus.
    """
    return os.environ.get("ENGRAM_WATCHER_MODEL", DEFAULT_WATCHER_MODEL)


def _watcher_timeout() -> int:
    """Resolve the per-call timeout (seconds) for the watcher's `claude -p`.

    Read from $ENGRAM_WATCHER_TIMEOUT each call so it can be tuned without
    restarting the watcher. Falls back to DEFAULT_WATCHER_TIMEOUT on a missing
    or non-positive / non-integer value.
    """
    raw = os.environ.get("ENGRAM_WATCHER_TIMEOUT", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WATCHER_TIMEOUT
    return val if val > 0 else DEFAULT_WATCHER_TIMEOUT


def _build_initial_prompt(cwd: str) -> str:
    return f"{build_watcher_prompt()}\n\nProject: {cwd}\n\n--- Session activity ---\n\n"


def _claude_p_new(message: str, schema: str) -> str:
    """Start a new watcher model session. Returns stdout, raises on failure.

    Uses --bare to skip hooks — prevents the watcher's own claude session from
    triggering SessionStart which would spawn another watcher (recursive fork
    bomb). Process failures (timeout/spawn error) raise so run_tick's retry path
    treats them as a held window.
    """
    return _run(message, schema, resume=None)


def _claude_p_resume(session_id: str, message: str, schema: str) -> str:
    """Resume an existing watcher session. Returns stdout, raises on failure.

    Uses --bare to skip hooks (see _claude_p_new docstring).
    """
    return _run(message, schema, resume=session_id)


def _run(message: str, schema: str, resume: str | None) -> str:
    result = invoke_claude_agent(
        message,
        timeout=_watcher_timeout(),
        model=_watcher_model(),
        schema=schema,
        resume=resume,
        bare=True,
        claude_bin=CLAUDE_BIN,
    )
    if result.error:
        raise RuntimeError(result.error)
    return result.stdout


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
    """Extract and parse the JSON response from claude -p output.

    The model returns the response one of two ways:
      - via the StructuredOutput tool (when `--json-schema` is honored) →
        `result` field is already clean JSON.
      - as a text block with Markdown-fenced JSON (```json ... ```) →
        `result` still contains the fences; naive json.loads fails.
    This parser handles both and also tolerates trailing/leading prose.
    """
    result_text = parse_claude_json_output(stdout)
    if not result_text:
        return None
    candidates = _candidate_json_strings(result_text)
    for s in candidates:
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _candidate_json_strings(text: str) -> list[str]:
    """Yield plausible JSON snippets extracted from a mixed text/code-fenced response."""
    out: list[str] = []
    stripped = text.strip()
    if stripped:
        out.append(stripped)

    # ```json ... ``` or ``` ... ```
    fence_re = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
    for m in fence_re.finditer(text):
        inner = m.group(1).strip()
        if inner:
            out.append(inner)

    # Largest balanced {...} block, as a last resort.
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        out.append(text[first : last + 1].strip())

    return out


def _save_memory(mem: dict, cwd: str) -> None:
    """Save a memory by calling engram remember.

    Plumbs `cwd` through `--project-cwd` so the user's working directory
    (not the watcher's, which is wherever launchd/the spawn happened to put
    it) is used to compute the project slug for scope=project memories.
    """
    name = mem.get("name", "")
    body = mem.get("body", "")
    kind = mem.get("kind") or "hint"
    scope = mem.get("scope", "project")
    triggers = mem.get("triggers", [])
    paths = mem.get("paths", [])

    if not name or not body:
        return
    if not triggers and not paths:
        return

    argv = [body, "--kind", kind, "--scope", scope, "--name", name]
    if cwd:
        argv.extend(["--project-cwd", cwd])
    for t in triggers:
        if isinstance(t, str) and t.strip():
            argv.extend(["--trigger", t.strip()])
    for p in paths:
        if isinstance(p, str) and p.strip():
            argv.extend(["--path", p.strip()])
    remember_main(argv)
