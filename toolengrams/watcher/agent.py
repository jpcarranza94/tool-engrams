"""Watcher permissioned `claude -p` sessions: formation + evaluation.

A watcher session does its job by CALLING the engram CLI, not by returning a
constrained JSON schema the harness parses (see ADR-0001).

Mechanics mirror the consolidation agent: a sandbox work dir with a
`settings.local.json` that grants exactly the role's command surface, ENGRAM_DB
in the env, and ENGRAM_IN_WATCHER set so the session's own tool calls don't
recursively trigger engram hooks (the recursion guard). Every tick is a FRESH
`claude -p` call (ADR-0005) — the sandbox dir stays stable per (work session,
role) only so its transcripts stay out of work projects, the recursion guard
recognizes it, and `engram cleanup` can reap it cold. The user's real cwd is
handed to the model in the prompt so it can pass `--project-cwd` to
`engram remember` / `--session-id` to `engram judge`.

Safety is the **command surface**, not a schema:
    formation → `engram remember` only
    eval      → `engram judge` + `engram quarantine` only

Model selection lives in the engine adapter (claude-code: per-role env →
`$ENGRAM_WATCHER_MODEL` → sonnet); timeout via `$ENGRAM_WATCHER_TIMEOUT`
(engine-neutral wall-clock budget).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..engine import EngineRequest, SandboxSpec, get_engine
from ..engine.claude_code import DEFAULT_WATCHER_MODEL
from ..utils import WATCHER_CHILD_ENV, prepend_engram_bin, safe_filename_id

# Per-call wall-clock budget for the watcher's `claude -p`. Tool-calling is
# multi-round-trip, so a busy window can run long; the tick HOLDS the cursor and
# retries on error/timeout (see tick._retry_decision). Tune via
# $ENGRAM_WATCHER_TIMEOUT without restarting. 120s proved too tight for the
# eval role (read delta + one `engram judge` per pending surface) — it hit
# SKIP-GIVEUP on every window, so no judgments ever landed.
DEFAULT_WATCHER_TIMEOUT = 300

# The command surface each role is granted in its sandbox. This — not a JSON
# schema — is what keeps a per-turn judge from nuking a good memory: eval can
# only judge surfaces and (reversibly, audited) quarantine by id. Neutral
# prefixes; the engine adapter translates them to its native grant form
# (claude-code: `Bash(<prefix> *)`), and the same prefixes' verbs feed the
# engine-agnostic $ENGRAM_ALLOWED_VERBS dispatch guard.
ROLE_COMMAND_PREFIXES: dict[str, tuple[str, ...]] = {
    "formation": ("engram remember",),
    # quarantine is eval's emergency brake (ADR-0007): id-only, soft-demote,
    # audited — structurally weaker than `forget`, which stays off-list.
    "eval": ("engram judge", "engram quarantine"),
}

# The defense-in-depth twin of the prefixes above: the engram CLI itself
# refuses any other subcommand when this env var is set (see __main__.py).
ROLE_ALLOWED_VERBS: dict[str, str] = {
    "formation": "remember",
    "eval": "judge,quarantine",
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

    Stability is bookkeeping, not session state: one dir per (session, role)
    keeps the watcher's own transcripts in a recognizable internal slug (the
    recursion guard and the nightly collect exclude it) and gives `engram
    cleanup` one cold thing to reap. The session id is sanitized so a hostile
    or malformed id can't traverse out of the sandbox root.
    """
    return _sandbox_root() / f"{_WORKDIR_PREFIX[role]}{safe_filename_id(work_session_id)}"


@dataclass(slots=True)
class SessionResult:
    """Outcome of one watcher `claude -p` call. `ok` is False on any process
    failure (timeout, spawn error, non-zero exit); the tick treats that as a held
    window and retries.
    Cost/token fields come from the JSON envelope of a successful call (None on
    failure — there is no envelope to read) and land on the `watcher_runs` row."""

    ok: bool
    error: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


def _watcher_model(role: str | None = None) -> str | None:
    """The model the active engine will use for `role` — kept here because the
    tick stamps it on the run row; resolution itself lives in the adapter."""
    return get_engine().resolve_model(role)


def engine_available() -> bool:
    """Is the active engine's binary reachable? Checked at call time (PATH may
    not be set yet at import under launchd)."""
    return get_engine().is_available()


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


def run_watcher_session(role: str, message: str,
                        work_session_id: str, run_id: int | None = None,
                        delta: str = "") -> SessionResult:
    """Run one FRESH permissioned `claude -p` call for `role` (ADR-0005).

    Ensures the (work session, role) stable sandbox cwd granting the role's
    allowlist, writes the transcript `delta` to `./delta.txt` there (the prompt
    tells the model to read it), sets ENGRAM_DB + ENGRAM_IN_WATCHER +
    ENGRAM_ORIGIN_SESSION (and ENGRAM_RUN_ID, when given) in the env, and
    shells out via the shared seam. Returns `ok=False` (held window) on any
    process failure. The side effects happen in-band via the model's `engram`
    calls — there is nothing to parse.
    """
    engine = get_engine()
    if not engine.is_available():
        return SessionResult(ok=False, error=f"{engine.NAME} CLI not found")

    prefixes = ROLE_COMMAND_PREFIXES.get(role)
    if prefixes is None:
        return SessionResult(ok=False, error=f"unknown role {role!r}")

    try:
        work_dir = _work_dir(role, work_session_id)
        work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # exist_ok accepts a pre-existing path silently — refuse one that isn't
        # a plain directory we own (symlink swap / squatting): the sandbox holds
        # transcript excerpts and the settings.local.json permission boundary.
        info = work_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return SessionResult(
                ok=False,
                error=f"sandbox is not a directory we own: {work_dir}")
        delta_path = work_dir / DELTA_FILENAME
        delta_path.write_text(delta or "(no new activity)")
        # Grant read access to exactly that file alongside the role's verbs.
        engine.prepare_sandbox(work_dir, SandboxSpec(
            command_prefixes=prefixes,
            readable_paths=(str(delta_path),),
        ))
        env = prepend_engram_bin(os.environ.copy())
        env[WATCHER_CHILD_ENV] = "1"
        env["ENGRAM_DB"] = str(db.db_path())
        # Defense in depth alongside the engine sandbox: the engram CLI itself
        # refuses subcommands outside the role's verbs (see __main__.py).
        env["ENGRAM_ALLOWED_VERBS"] = ROLE_ALLOWED_VERBS[role]
        # Attribution for same-session suppression (ADR-0006): the child's
        # `engram remember` reads this — attribution never depends on the
        # model passing --origin-session itself.
        env["ENGRAM_ORIGIN_SESSION"] = work_session_id
        if run_id is not None:
            env["ENGRAM_RUN_ID"] = str(run_id)
        result = engine.invoke(EngineRequest(
            prompt=message,
            timeout=_watcher_timeout(),
            role=role,
            cwd=str(work_dir),
            env=env,
        ))
    except Exception as e:
        # Sandbox setup failed (mkdir / delta write / settings). Fail open so
        # the tick finalizes the run row as error instead of crashing.
        return SessionResult(ok=False, error=f"session setup failed: {e}")

    if not result.ok:
        return SessionResult(ok=False,
                             error=result.error or f"exit {result.returncode}")
    # The engine's own accounting — captured here so the run row (and
    # `engram monitor`) can show watcher spend.
    return SessionResult(
        ok=True,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
    )
