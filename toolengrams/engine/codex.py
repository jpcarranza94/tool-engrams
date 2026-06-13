"""Codex engine adapter: `codex exec` as the headless runner.

Codex's project config and execpolicy files are trust-gated in fresh work
dirs, so the sandbox boundary is expressed entirely as runtime `-c`
overrides on the `codex exec` command. The engine-agnostic
`$ENGRAM_ALLOWED_VERBS` guard remains the command-surface backstop for watcher
formation/eval children.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import db, paths
from .interface import EngineRequest, SandboxSpec
from .result import EngineResult

NAME = "codex"

_ROLE_MODEL_ENV = {
    "formation": "ENGRAM_CODEX_FORMATION_MODEL",
    "eval": "ENGRAM_CODEX_EVAL_MODEL",
}


def is_available() -> bool:
    """Resolved at call time because PATH may be minimal under launchd/cron."""
    return shutil.which("codex") is not None


def resolve_model(role: str | None = None) -> str | None:
    """Codex model chain per role.

    Per-role env ($ENGRAM_CODEX_FORMATION_MODEL / $ENGRAM_CODEX_EVAL_MODEL)
    beats $ENGRAM_CODEX_WATCHER_MODEL. No fallback model is passed: Codex's
    own ~/.codex/config.toml/catalog default should apply. Consolidation also
    returns None so it uses the user's configured default.
    """
    if role == "consolidation":
        return None
    per_role = os.environ.get(_ROLE_MODEL_ENV.get(role or "", ""), "") if role else ""
    if per_role:
        return per_role
    return os.environ.get("ENGRAM_CODEX_WATCHER_MODEL") or None


def prepare_sandbox(work_dir: Path, spec: SandboxSpec) -> None:
    """No project-local codex files are written.

    Fresh watcher/consolidation work dirs are untrusted by Codex, which means
    `.codex/config.toml` and `.codex/rules/` would be silently ignored. The
    neutral spec is enforced at invoke time with `-c` sandbox overrides plus
    the caller-set `$ENGRAM_ALLOWED_VERBS` guard.
    """
    work_dir.mkdir(parents=True, exist_ok=True)


def invoke(req: EngineRequest) -> EngineResult:
    """Run `codex exec --json ... -- <prompt>`. Never raises."""
    binary = shutil.which("codex")
    if not binary:
        return EngineResult(ok=False, engine=NAME, returncode=1,
                            error="codex CLI not found on PATH")

    work_dir = Path(req.cwd or os.getcwd())
    db_dir = db.db_path().parent
    schema_path: Path | None = None
    last_message_path: Path | None = None

    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = paths.engram_home()
        temp_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = _temp_path(temp_dir, ".txt")
        if req.schema:
            schema_path = _temp_path(temp_dir, ".schema.json")
            schema_path.write_text(req.schema)

        model = req.model if req.model is not None else resolve_model(req.role)
        argv = _argv(binary, work_dir, db_dir, last_message_path, model,
                     schema_path, req.prompt)

        proc = subprocess.run(
            argv, cwd=str(work_dir), env=req.env,
            capture_output=True, text=True, timeout=req.timeout,
        )
        text = _read_last_message(last_message_path)
    except subprocess.TimeoutExpired:
        return EngineResult(ok=False, engine=NAME, returncode=1, timed_out=True,
                            error=f"codex exec timed out ({req.timeout}s)")
    except Exception as e:
        return EngineResult(ok=False, engine=NAME, returncode=1,
                            error=f"failed to spawn codex: {e}")
    finally:
        _unlink(schema_path)
        _unlink(last_message_path)

    stdout = proc.stdout or ""
    usage = _usage(stdout) or {}
    return EngineResult(
        ok=proc.returncode == 0,
        engine=NAME,
        stdout=stdout,
        returncode=proc.returncode,
        error=_error(proc.returncode, proc.stderr, stdout),
        text=text,
        cost_usd=None,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cached_input_tokens"),
        cache_creation_tokens=None,
    )


def _argv(
    binary: str,
    work_dir: Path,
    db_dir: Path,
    last_message_path: Path,
    model: str | None,
    schema_path: Path | None,
    prompt: str,
) -> list[str]:
    configs = [
        "sandbox_workspace_write.writable_roots="
        + json.dumps([str(db_dir), str(work_dir)], separators=(",", ":")),
        "sandbox_workspace_write.network_access=false",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        'approval_policy="never"',
    ]
    argv = [
        binary, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
        "-s", "workspace-write",
    ]
    for config in configs:
        argv += ["-c", config]
    argv += ["--cd", str(work_dir)]
    if model:
        argv += ["-m", model]
    if schema_path is not None:
        argv += ["--output-schema", str(schema_path)]
    argv += ["-o", str(last_message_path), "--", prompt]
    return argv


def _temp_path(temp_dir: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="codex-engine-", suffix=suffix,
                                dir=str(temp_dir))
    os.close(fd)
    return Path(name)


def _read_last_message(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _usage(stdout: str) -> dict | None:
    usage = None
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "turn.completed":
            value = payload.get("usage")
            if isinstance(value, dict):
                usage = value
    return usage


def _event_error(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "error":
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str) and message:
                return message
        if payload.get("type") == "turn.failed":
            message = payload.get("message") or payload.get("error")
            if isinstance(message, dict):
                message = message.get("message") or message.get("detail")
            if isinstance(message, str) and message:
                return message
    return None


def _error(returncode: int, stderr: str | None, stdout: str) -> str | None:
    if returncode == 0:
        return None
    error = f"exit {returncode}"
    detail = (stderr or "").strip().replace("\n", " ") or _event_error(stdout)
    if detail:
        error += f": {detail[-300:]}"
    return error


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
