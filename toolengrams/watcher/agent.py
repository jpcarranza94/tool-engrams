"""Watcher permissioned `claude -p` sessions: formation + evaluation.

A watcher session does its job by CALLING the engram CLI, not by returning a
constrained JSON schema the harness parses (see ADR-0001).

Mechanics mirror the consolidation agent: a sandbox work dir with a
`settings.local.json` that grants exactly the role's command surface, ENGRAM_DB
in the env, and ENGRAM_IN_WATCHER set so the session's own tool calls don't
recursively trigger engram hooks (the recursion guard). The sandbox dir is
STABLE per (work session, role) — `claude -p --resume` resolves session ids
within the current project (cwd), so a fresh dir per tick would orphan every
resume id the moment it was extracted. The
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
# $ENGRAM_WATCHER_TIMEOUT without restarting. 120s proved too tight for the
# eval role (read delta + one `engram judge` per pending surface) — it hit
# SKIP-GIVEUP on every window, so no judgments ever landed.
DEFAULT_WATCHER_TIMEOUT = 300

# The command surface each role is granted in its sandbox settings. This — not a
# JSON schema — is what keeps a per-turn judge from nuking a good memory: eval
# literally cannot run anything but `engram judge`.
# Space-glob form mirrors the consolidation agent's proven `Bash(engram *)`
# grant — restricted here to the single verb each role is allowed to run.
ROLE_ALLOWLIST: dict[str, list[str]] = {
    "formation": ["Bash(engram remember *)"],
    "eval": ["Bash(engram judge *)"],
}

# Sandbox-dir prefix per role — must stay in sync with
# hooks/_skip._INTERNAL_CWD_PREFIXES (recursion guard) and
# consolidation/collect._INTERNAL_PROJECT_RE (keeps watcher transcripts out of
# the nightly review).
_WORKDIR_PREFIX = {"formation": "engram-formation-", "eval": "engram-eval-"}


def _work_dir(role: str, work_session_id: str) -> Path:
    """The stable sandbox cwd for one (work session, role).

    Stability is what makes `--resume` work: Claude Code stores conversations
    under a per-project slug derived from the cwd, and `--resume <id>` only
    resolves within the current project. The dir lives under the system temp
    root, so the OS reaps it once the session goes cold; if it vanishes while a
    resume id is still persisted, the resume fails and tick.py falls back to a
    fresh session on the retry.
    """
    return Path(tempfile.gettempdir()) / f"{_WORKDIR_PREFIX[role]}{work_session_id}"


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


# Filename of the transcript delta dropped into the session's sandbox cwd. The
# model reads/greps it instead of receiving the (potentially huge) delta inline —
# smaller argv, and the model can be selective on big windows. tick.py's prompt
# pointers interpolate this constant; the packaged prompt defaults
# (prompts/defaults/{watcher,eval}.md) hardcode "./delta.txt" and must be kept in
# sync by hand if this ever changes.
DELTA_FILENAME = "delta.txt"


def run_watcher_session(role: str, message: str, resume: str | None,
                        work_session_id: str, run_id: int | None = None,
                        delta: str = "") -> SessionResult:
    """Run one permissioned `claude -p` turn for `role`, resuming `resume` if set.

    Ensures the (work session, role) stable sandbox cwd granting the role's
    allowlist, writes the transcript `delta` to `./delta.txt` there (the prompt
    tells the model to read it), sets ENGRAM_DB + ENGRAM_IN_WATCHER (and
    ENGRAM_RUN_ID, when given) in the env, and shells out via the shared seam.
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

    try:
        work_dir = _work_dir(role, work_session_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        delta_path = work_dir / DELTA_FILENAME
        delta_path.write_text(delta or "(no new activity)")
        # Grant read access to exactly that file alongside the role's one verb.
        # `allow + [...]` builds a NEW list — never mutate the module ROLE_ALLOWLIST.
        write_agent_settings(Path(work_dir), allow + [f"Read({delta_path})"])
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
            cwd=str(work_dir),
            env=env,
            claude_bin=CLAUDE_BIN,
        )
    except Exception as e:
        # Sandbox setup failed (mkdir / delta write / settings). Fail open so
        # the tick finalizes the run row as error instead of crashing.
        return SessionResult(ok=False, watcher_session_id=resume,
                             error=f"session setup failed: {e}")

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
