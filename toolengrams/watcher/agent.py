"""Watcher permissioned `claude -p` sessions: formation + evaluation.

A watcher session does its job by CALLING the engram CLI, not by returning a
constrained JSON schema the harness parses (see ADR-0001).

Mechanics mirror the consolidation agent: a temp work dir with a
`settings.local.json` that grants exactly the role's command surface, ENGRAM_DB
in the env, and ENGRAM_IN_WATCHER set so the session's own tool calls don't
recursively trigger engram hooks (the recursion guard). The
user's real cwd is handed to the model in the prompt so it can pass
`--project-cwd` to `engram remember` / `--session-id` to `engram judge`.

Safety is the **command surface**, not a schema:
    formation → `engram remember` only
    eval      → `engram judge` only

Model selection via `$ENGRAM_WATCHER_MODEL` (default opus); timeout via
`$ENGRAM_WATCHER_TIMEOUT`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..claude_invoke import invoke_claude_agent, write_agent_settings
from ..utils import WATCHER_CHILD_ENV

CLAUDE_BIN = shutil.which("claude")

DEFAULT_WATCHER_MODEL = "opus"

# Per-call wall-clock budget for the watcher's `claude -p`. Tool-calling is
# multi-round-trip, so a busy window can run long; the tick HOLDS the cursor and
# retries on error/timeout (see tick._retry_decision). Tune via
# $ENGRAM_WATCHER_TIMEOUT without restarting.
DEFAULT_WATCHER_TIMEOUT = 120

# The command surface each role is granted in its sandbox settings. This — not a
# JSON schema — is what keeps a per-turn judge from nuking a good memory: eval
# literally cannot run anything but `engram judge`.
# Space-glob form mirrors the consolidation agent's proven `Bash(engram *)`
# grant — restricted here to the single verb each role is allowed to run.
ROLE_ALLOWLIST: dict[str, list[str]] = {
    "formation": ["Bash(engram remember *)"],
    "eval": ["Bash(engram judge *)"],
}

# Temp-dir prefix per role — must stay in sync with hooks/_skip._INTERNAL_CWD_PREFIXES
# so the watcher session's own cwd is recognized as internal (recursion guard).
_WORKDIR_PREFIX = {"formation": "engram-formation-", "eval": "engram-eval-"}


@dataclass(slots=True)
class SessionResult:
    """Outcome of one watcher `claude -p` turn. `ok` is False on any process
    failure (timeout, spawn error, non-zero exit); the tick treats that as a held
    window and retries. `watcher_session_id` is the id to `--resume` next time."""

    ok: bool
    watcher_session_id: str | None
    error: str | None = None


def _watcher_model() -> str:
    """Resolve the watcher model. Read each call so overrides apply live."""
    return os.environ.get("ENGRAM_WATCHER_MODEL", DEFAULT_WATCHER_MODEL)


def _watcher_timeout() -> int:
    """Resolve the per-call timeout (seconds). Read each call; fall back on bad."""
    raw = os.environ.get("ENGRAM_WATCHER_TIMEOUT", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WATCHER_TIMEOUT
    return val if val > 0 else DEFAULT_WATCHER_TIMEOUT


def run_watcher_session(role: str, message: str, resume: str | None,
                        run_id: int | None = None) -> SessionResult:
    """Run one permissioned `claude -p` turn for `role`, resuming `resume` if set.

    Builds a throwaway sandbox cwd granting the role's allowlist, sets ENGRAM_DB
    + ENGRAM_IN_WATCHER (and ENGRAM_RUN_ID, when given, so the model's `engram`
    CLI calls record run events) in the env, and shells out via the shared seam.
    Returns `ok=False` (held window) on any process failure. The side effects
    happen in-band via the model's `engram` calls — there is nothing to parse.
    """
    if not CLAUDE_BIN:
        return SessionResult(ok=False, watcher_session_id=resume,
                             error="claude CLI not found")

    allow = ROLE_ALLOWLIST.get(role)
    if allow is None:
        return SessionResult(ok=False, watcher_session_id=resume,
                             error=f"unknown role {role!r}")

    work_dir = tempfile.mkdtemp(prefix=_WORKDIR_PREFIX[role])
    try:
        write_agent_settings(Path(work_dir), allow)
        env = os.environ.copy()
        env[WATCHER_CHILD_ENV] = "1"
        env["ENGRAM_DB"] = str(db.db_path())
        if run_id is not None:
            env["ENGRAM_RUN_ID"] = str(run_id)
        result = invoke_claude_agent(
            message,
            timeout=_watcher_timeout(),
            model=_watcher_model(),
            resume=resume,
            cwd=work_dir,
            env=env,
            claude_bin=CLAUDE_BIN,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if result.error or result.returncode != 0:
        return SessionResult(ok=False, watcher_session_id=resume,
                             error=result.error or f"exit {result.returncode}")
    # Reuse the existing session for the next --resume. We extract the id from the
    # JSON envelope (always present) rather than pinning a caller --session-id:
    # extraction is the proven path and composes with the permissioned session.
    sid = _extract_session_id(result.stdout) or resume
    return SessionResult(ok=True, watcher_session_id=sid)


def _extract_session_id(stdout: str) -> str | None:
    """Extract session_id from `claude -p --output-format json` output."""
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
