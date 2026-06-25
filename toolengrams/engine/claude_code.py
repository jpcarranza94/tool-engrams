"""Claude Code engine adapter: `claude -p` as the headless runner.

Absorbs the old `toolengrams/claude_invoke.py` seam plus the pieces that
were claude-specific in its callers: the result-envelope usage parsing
(was `watcher/agent._envelope`), the model-resolution chain (was
`watcher/agent._watcher_model`), and the settings.local.json permission
writer. The adapter is the module — see `engine/interface.py`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .interface import EngineRequest, SandboxSpec
from .result import EngineResult

NAME = "claude-code"
min_version = None

# Sonnet, not opus: judging surfaced memories and extracting tool lessons from
# a transcript delta is not opus-grade work, and the watcher makes ~dozens of
# background calls a day. Override per tick via $ENGRAM_WATCHER_MODEL, or per
# role via $ENGRAM_FORMATION_MODEL / $ENGRAM_EVAL_MODEL — formation (pattern
# extraction) and eval (verdict judging) have different difficulty/cost
# profiles, so they can run different models.
DEFAULT_WATCHER_MODEL = "sonnet"

_ROLE_MODEL_ENV = {"formation": "ENGRAM_FORMATION_MODEL",
                   "eval": "ENGRAM_EVAL_MODEL"}

# The consolidation agent's broad read-only surface (`readonly_explore`):
# file reading/search plus the inspection commands its prompt leans on.
# Read-only git so the agent can compare memory bodies against current repo
# state (git-aware staleness audit).
_EXPLORE_ALLOW = [
    "Read", "Grep", "Glob",
]
_EXPLORE_BASH_PREFIXES = (
    "sqlite3", "wc", "head", "cat", "ls",
    "git log", "git diff", "git show", "git -C", "git rev-parse",
)


def is_available() -> bool:
    """Resolved at call time, not import time: module-level shutil.which()
    fails when the module is imported before PATH is fully set (launchd's
    minimal environment)."""
    return shutil.which("claude") is not None


def installed_version() -> str | None:
    """Parsed x.y.z from `claude --version`, or None."""
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _version_from_text(out)


def resolve_model(role: str | None = None) -> str | None:
    """Model chain per role: per-role env ($ENGRAM_FORMATION_MODEL /
    $ENGRAM_EVAL_MODEL) → watcher-wide ($ENGRAM_WATCHER_MODEL) → sonnet.
    Consolidation returns None — no --model flag, claude's configured
    default (opus-grade review is the point there). Read each call so
    overrides apply live."""
    if role == "consolidation":
        return None
    per_role = os.environ.get(_ROLE_MODEL_ENV.get(role or "", ""), "") if role else ""
    if per_role:
        return per_role
    return os.environ.get("ENGRAM_WATCHER_MODEL", DEFAULT_WATCHER_MODEL)


def prepare_sandbox(work_dir: Path, spec: SandboxSpec) -> None:
    """Write .claude/settings.local.json translating the neutral spec into
    claude's allowlist grammar: command prefixes become `Bash(<prefix> *)`,
    readable paths become `Read(<path>)`."""
    # readonly_explore is checked twice ON PURPOSE: the split preserves the
    # historic allowlist byte order (file tools, role commands, then the
    # explore bash surface) that the consolidation grant test pins.
    allow: list[str] = []
    if spec.readonly_explore:
        allow += _EXPLORE_ALLOW
    allow += [f"Bash({p} *)" for p in spec.command_prefixes]
    if spec.readonly_explore:
        allow += [f"Bash({p} *)" for p in _EXPLORE_BASH_PREFIXES]
    allow += [f"Read({p})" for p in spec.readable_paths]
    settings_dir = work_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings = {"permissions": {"allow": allow}}
    (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))


def invoke(req: EngineRequest) -> EngineResult:
    """Run `claude -p <prompt>` with `--output-format json`. Never raises:
    timeout and spawn failure come back on the result."""
    binary = shutil.which("claude")
    if not binary:
        return EngineResult(ok=False, engine=NAME, returncode=1,
                            error="claude CLI not found on PATH")

    model = req.model if req.model is not None else resolve_model(req.role)
    argv = [binary, "-p"]
    if model:
        argv += ["--model", model]
    # Continue the prior conversation (same context, same cwd/sandbox) instead
    # of opening a fresh one — used to ask the agent to re-emit a malformed
    # report JSON block in place.
    if req.resume_session_id:
        argv += ["--resume", req.resume_session_id]
    argv += ["--output-format", "json"]
    if req.schema:
        argv += ["--json-schema", req.schema]
    argv += ["--", req.prompt]

    try:
        proc = subprocess.run(
            argv, cwd=req.cwd, env=req.env,
            # Headless runners take their prompt as an argv positional but may
            # still read a non-TTY stdin; a detached watcher tick inherits an
            # open pipe and would block. Hand EOF so the prompt is authoritative.
            # (See the codex adapter for the concrete hang this prevents.)
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=req.timeout,
        )
    except subprocess.TimeoutExpired:
        return EngineResult(ok=False, engine=NAME, returncode=1, timed_out=True,
                            error=f"claude -p timed out ({req.timeout}s)")
    except Exception as e:
        return EngineResult(ok=False, engine=NAME, returncode=1,
                            error=f"failed to spawn claude: {e}")

    # On a non-zero exit, surface the real reason: the stderr tail (rate-limit,
    # auth, …). Without this a plain `exit N` hides what happened.
    error = None
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().replace("\n", " ")
        error = f"exit {proc.returncode}"
        if stderr:
            error += f": {stderr[-300:]}"

    stdout = proc.stdout or ""
    payload = _envelope(stdout) or {}
    usage = payload.get("usage") or {}
    return EngineResult(
        ok=proc.returncode == 0,
        engine=NAME,
        stdout=stdout,
        returncode=proc.returncode,
        error=error,
        text=_extract_text(stdout),
        session_id=payload.get("session_id"),
        cost_usd=payload.get("total_cost_usd"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
    )


def _extract_text(stdout: str) -> str:
    """The response text from `--output-format json` output. With
    --json-schema, the constrained JSON is in `structured_output` (already a
    dict) and beats the free-form `result` summary."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            so = payload.get("structured_output")
            if so is not None:
                return json.dumps(so) if isinstance(so, dict) else str(so)
            return payload.get("result", "")
    return ""


def _envelope(stdout: str) -> dict | None:
    """The result envelope from `claude -p --output-format json`: the first
    parseable JSON line carrying a session_id. Reports the call's exact cost
    and token usage."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("session_id"):
                return payload
    return None


def _version_from_text(text: str) -> str | None:
    match = re.search(r"\d+\.\d+\.\d+", text or "")
    return match.group(0) if match else None
