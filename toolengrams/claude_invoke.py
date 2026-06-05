"""The single seam for invoking `claude -p` as an agent.

Both the event-driven watcher (per-tick memory formation) and the nightly
consolidation agent shell out to `claude -p`. They differ in flags, timeout,
cwd/env, and how they read the response — but the *process mechanics* (resolve
the binary, build argv, run, handle timeout / spawn failure) are identical.

`invoke_claude_agent` owns those mechanics and returns the raw stdout in a
`ClaudeResult`; each caller owns response interpretation (the watcher's
schema-constrained parse vs. consolidation's free-text report). `parse_claude_
json_output` is the small shared reader both happen to use; `write_agent_
settings` preps an agent's permission sandbox.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ClaudeResult:
    """Outcome of one `claude -p` invocation. Process failures (timeout, spawn
    error, missing binary) are returned as flags here, never raised — callers
    decide whether that's fatal."""

    stdout: str
    returncode: int
    timed_out: bool = False
    error: str | None = None


def invoke_claude_agent(
    prompt: str,
    *,
    timeout: int,
    model: str | None = None,
    schema: str | None = None,
    resume: str | None = None,
    bare: bool = False,
    cwd: str | None = None,
    env: dict | None = None,
    claude_bin: str | None = None,
) -> ClaudeResult:
    """Run `claude -p <prompt>` with `--output-format json` and capture stdout.

    Flags are added only when their argument is given: `--bare` (skip hooks),
    `--model`, `--json-schema` (constrained decoding), `--resume`. The binary is
    resolved at call time via `shutil.which` unless `claude_bin` is supplied (a
    caller that already resolved it — e.g. at import — passes it to skip the
    lookup). Never raises: timeout and spawn failure come back on `ClaudeResult`.
    """
    binary = claude_bin or shutil.which("claude")
    if not binary:
        return ClaudeResult(stdout="", returncode=1, error="claude CLI not found on PATH")

    argv = [binary, "-p"]
    if bare:
        argv.append("--bare")
    if model:
        argv += ["--model", model]
    argv += ["--output-format", "json"]
    if schema:
        argv += ["--json-schema", schema]
    if resume:
        argv += ["--resume", resume]
    argv += ["--", prompt]

    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult(stdout="", returncode=1, timed_out=True,
                            error=f"claude -p timed out ({timeout}s)")
    except Exception as e:
        return ClaudeResult(stdout="", returncode=1, error=f"failed to spawn claude: {e}")

    return ClaudeResult(stdout=proc.stdout or "", returncode=proc.returncode)


def parse_claude_json_output(stdout: str) -> str:
    """Extract the result text from claude -p --output-format json output.

    When --json-schema is used, the constrained JSON response is in the
    `structured_output` field (already a dict). The `result` field contains
    free-form text summary which is NOT the structured data.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                # Prefer structured_output (from --json-schema constrained decoding).
                so = payload.get("structured_output")
                if so is not None:
                    return json.dumps(so) if isinstance(so, dict) else str(so)
                return payload.get("result", "")
            except json.JSONDecodeError:
                continue
    return ""


def write_agent_settings(work_dir: Path, permissions: list[str]) -> None:
    """Write .claude/settings.local.json granting specified permissions."""
    settings_dir = work_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings = {"permissions": {"allow": permissions}}
    (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))
