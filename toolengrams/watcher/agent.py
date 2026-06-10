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

Model selection via `$ENGRAM_WATCHER_MODEL` (default sonnet); timeout via
`$ENGRAM_WATCHER_TIMEOUT`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..claude_invoke import invoke_claude_agent, write_agent_settings
from ..utils import WATCHER_CHILD_ENV, prepend_engram_bin, safe_filename_id

CLAUDE_BIN = shutil.which("claude")

# Sonnet, not opus: judging surfaced memories and extracting tool lessons from
# a transcript delta is not opus-grade work, and the watcher makes ~dozens of
# background calls a day. Override per tick via $ENGRAM_WATCHER_MODEL, or per
# role via $ENGRAM_FORMATION_MODEL / $ENGRAM_EVAL_MODEL — formation (pattern
# extraction) and eval (verdict judging) have different difficulty/cost
# profiles, so they can run different models.
DEFAULT_WATCHER_MODEL = "sonnet"

_ROLE_MODEL_ENV = {"formation": "ENGRAM_FORMATION_MODEL",
                   "eval": "ENGRAM_EVAL_MODEL"}

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


def _sandbox_root() -> Path:
    """Root for the stable sandbox cwds. Keyed off the DB dir rather than the
    system temp root: it's user-only territory (no shared-/tmp dir squatting),
    and it follows $ENGRAM_DB so tests isolate automatically. Nothing reaps it
    but us — the once-daily `engram cleanup` removes cold sandboxes."""
    return db.db_path().parent / "sandboxes"


def _work_dir(role: str, work_session_id: str) -> Path:
    """The stable sandbox cwd for one (work session, role).

    Stability is what makes `--resume` work: Claude Code stores conversations
    under a per-project slug derived from the cwd *path string*, and
    `--resume <id>` only resolves within the current project — so the path must
    be the same every tick (recreating a deleted dir yields the same slug and
    still resolves). What does orphan a persisted resume id is Claude Code's
    own transcript cleanup; tick.py then falls back to a fresh session on the
    retry. The session id is sanitized so a hostile or malformed id can't
    traverse out of the sandbox root.
    """
    return _sandbox_root() / f"{_WORKDIR_PREFIX[role]}{safe_filename_id(work_session_id)}"


@dataclass(slots=True)
class SessionResult:
    """Outcome of one watcher `claude -p` turn. `ok` is False on any process
    failure (timeout, spawn error, non-zero exit); the tick treats that as a held
    window and retries. `watcher_session_id` is the id to `--resume` next time.
    Cost/token fields come from the JSON envelope of a successful call (None on
    failure — there is no envelope to read) and land on the `watcher_runs` row."""

    ok: bool
    watcher_session_id: str | None
    error: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


def _watcher_model(role: str | None = None) -> str:
    """Resolve the model for a role: per-role env ($ENGRAM_FORMATION_MODEL /
    $ENGRAM_EVAL_MODEL) → watcher-wide ($ENGRAM_WATCHER_MODEL) → default.
    Read each call so overrides apply live."""
    per_role = os.environ.get(_ROLE_MODEL_ENV.get(role or "", ""), "") if role else ""
    if per_role:
        return per_role
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
        work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # exist_ok accepts a pre-existing path silently — refuse one that isn't
        # a plain directory we own (symlink swap / squatting): the sandbox holds
        # transcript excerpts and the settings.local.json permission boundary.
        info = work_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return SessionResult(
                ok=False, watcher_session_id=resume,
                error=f"sandbox is not a directory we own: {work_dir}")
        delta_path = work_dir / DELTA_FILENAME
        delta_path.write_text(delta or "(no new activity)")
        # Grant read access to exactly that file alongside the role's one verb.
        # `allow + [...]` builds a NEW list — never mutate the module ROLE_ALLOWLIST.
        write_agent_settings(Path(work_dir), allow + [f"Read({delta_path})"])
        env = prepend_engram_bin(os.environ.copy())
        env[WATCHER_CHILD_ENV] = "1"
        env["ENGRAM_DB"] = str(db.db_path())
        if run_id is not None:
            env["ENGRAM_RUN_ID"] = str(run_id)
        result = invoke_claude_agent(
            message,
            timeout=_watcher_timeout(),
            model=_watcher_model(role),
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
    # The same envelope reports the call's exact cost and token usage — captured
    # here so the run row (and `engram monitor`) can show watcher spend.
    payload = _envelope(result.stdout) or {}
    usage = payload.get("usage") or {}
    return SessionResult(
        ok=True,
        watcher_session_id=payload.get("session_id") or resume,
        cost_usd=payload.get("total_cost_usd"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_tokens=usage.get("cache_creation_input_tokens"),
    )


def _envelope(stdout: str) -> dict | None:
    """The result envelope from `claude -p --output-format json`: the first
    parseable JSON line carrying a session_id."""
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
