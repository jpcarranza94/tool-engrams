"""Event-driven watcher tick — formation + evaluation.

Hooks fire a detached `engram watcher-tick` per meaningful event. Each tick reads
the transcript delta since its (session, role) cursor, decides whether the role
has work to do, and if so runs a permissioned `claude -p` session that does its
job by calling the engram CLI (`engram remember` for formation, `engram judge`
for evaluation). No JSON schema, no parsing — the side effects happen in-band.

Two roles share this engine:
  - **formation** — Stop / flush / recovery. Gates out pure-chat turns (unless
    armed by a prior failure). Creates memories.
  - **evaluation** — Stop / flush, only when the session has pending (unjudged)
    surfaces. Reads FORWARD from its own trailing cursor and judges how each
    surfaced memory fared. Defers by not judging; flush forces closure.

State (cursor / resume id / armed / fail_streak) lives per `(work_session_id,
role)` in `watcher_state`, behind `state.py`. Ticks for the same (session, role)
are serialized by a non-blocking file lock; the two roles run concurrently.

Tail recovery: a session can die before its final Stop/flush. `sweep_idle_
sessions` (run from SessionStart) re-fires a formation flush — and an eval flush
if surfaces are still pending — for any abandoned session.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..prompts.eval import EVAL_SUBSEQUENT_HEADER, build_eval_prompt
from ..prompts.watcher import WATCHER_SUBSEQUENT_HEADER, build_watcher_prompt
from ..retrieval import session_state
from ..utils import WATCHER_CHILD_ENV
from . import runs_store, state
from .agent import CLAUDE_BIN, _watcher_model, run_watcher_session
from .log import LOG_PATH, _log
from .state import ensure_row
from .transcript_format import _format_delta, _read_lines_from

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = sys.executable

# How many consecutive failed attempts on the SAME transcript window before we
# give up and advance past it. A failure (process error/timeout) HOLDS the cursor
# and retries next tick. `fail_streak` is persisted in watcher_state, so the
# count carries across the independent per-event tick processes.
MAX_FORM_RETRIES = 3

# Minimum seconds between ticks for one (session, role). A burst of triggers
# coalesces into one model call over the accumulated delta. A debounce, NOT a
# poll. Flush triggers ignore it. Tunable via env.
DEFAULT_TICK_COALESCE_SEC = 45

# A tracked session whose last tick is older than this (and which still has
# unread lines) is treated as abandoned; its tail is recovered at the next
# SessionStart. Tunable via env.
DEFAULT_IDLE_SWEEP_SEC = 1800


def _retry_decision(failed: bool, fail_streak: int, max_attempts: int) -> tuple[bool, int]:
    """Decide whether to advance the cursor after a tick.

    Returns (advance_cursor, new_fail_streak).
      - success            → advance, reset streak to 0.
      - failure, streak<max → HOLD (don't advance), bump streak (retry window).
      - failure, streak>=max → give up: advance past the window, reset streak.
    """
    if not failed:
        return True, 0
    streak = fail_streak + 1
    if streak >= max_attempts:
        return True, 0
    return False, streak


def _coalesce_sec() -> int:
    raw = os.environ.get("ENGRAM_TICK_COALESCE_SEC", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TICK_COALESCE_SEC
    return val if val >= 0 else DEFAULT_TICK_COALESCE_SEC


def _idle_sweep_sec() -> int:
    raw = os.environ.get("ENGRAM_IDLE_SWEEP_SEC", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_SWEEP_SEC
    return val if val > 0 else DEFAULT_IDLE_SWEEP_SEC


# ---------- hook-side helpers (cheap; run inside the hook process) ----------


def arm(session_id: str, transcript_path: str = "", cwd: str = "") -> None:
    """Mark the formation role 'armed' (a tool failure happened). The next turn-
    boundary formation tick runs the model even if that turn had no tool_use
    lines, so an error→fix episode is never gated out."""
    if transcript_path:
        ensure_row(session_id, transcript_path, cwd, role="formation")
    state.arm(session_id, role="formation")


def should_spawn(session_id: str, flush: bool, role: str = "formation") -> bool:
    """Coalesce gate (hook side): skip spawning a tick if one ran very recently
    for this (session, role), unless this is a flush."""
    if flush:
        return True
    return state.seconds_since_tick(session_id, role) >= _coalesce_sec()


def spawn_tick(session_id: str, transcript_path: str, cwd: str,
               flush: bool = False, role: str = "formation") -> None:
    """Fire-and-forget a detached `engram watcher-tick`. Returns immediately so
    the hook never blocks the user. ENGRAM_IN_WATCHER guards against the tick's
    own `claude` recursively triggering more ticks."""
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env[WATCHER_CHILD_ENV] = "1"
        argv = [PYTHON_BIN, "-m", "toolengrams", "watcher-tick",
                session_id, transcript_path, cwd, "--role", role]
        if flush:
            argv.append("--flush")
        subprocess.Popen(
            argv, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _log(f"TICK-SPAWN-ERROR session={session_id} role={role} error={e}")


def trigger(session_id: str, transcript_path: str, cwd: str,
            reason: str, flush: bool = False) -> None:
    """Hook convenience: coalesce-gate then spawn a detached FORMATION tick."""
    if not session_id or not transcript_path:
        return
    if should_spawn(session_id, flush, role="formation"):
        spawn_tick(session_id, transcript_path, cwd, flush=flush, role="formation")
    else:
        _log(f"TICK-COALESCED session={session_id} role=formation reason={reason}")


def trigger_eval(session_id: str, transcript_path: str, cwd: str,
                 reason: str, flush: bool = False) -> None:
    """Hook convenience: spawn an EVAL tick, but only when the session has
    pending (unjudged) surfaces — most turns surface nothing, so this bounds eval
    cost to the turns that need it. Then coalesce-gate (eval role) and spawn."""
    if not session_id or not transcript_path:
        return
    try:
        with db.session() as conn:
            if not session_state.has_pending_surfaces(conn, session_id):
                return
    except Exception:
        return
    if should_spawn(session_id, flush, role="eval"):
        spawn_tick(session_id, transcript_path, cwd, flush=flush, role="eval")
    else:
        _log(f"TICK-COALESCED session={session_id} role=eval reason={reason}")


def sweep_idle_sessions(current_session_id: str) -> int:
    """Backstop for lost tails: re-fire a formation flush (and an eval flush if
    surfaces are still pending) for every abandoned session (other than the
    current one). Run from SessionStart. Returns the number of sessions swept."""
    idle = state.sweep_idle(_idle_sweep_sec(), exclude_session_id=current_session_id,
                            limit=state.DEFAULT_SWEEP_LIMIT)
    for s in idle:
        spawn_tick(s.session_id, s.transcript_path, s.cwd, flush=True, role="formation")
        trigger_eval(s.session_id, s.transcript_path, s.cwd, reason="idle-sweep", flush=True)
    if idle:
        capped = " (capped — more may remain for the next SessionStart)" \
            if len(idle) >= state.DEFAULT_SWEEP_LIMIT else ""
        _log(f"IDLE-SWEEP recovered={len(idle)} from_session={current_session_id}{capped}")
    return len(idle)


# ---------- tick body (runs in the detached process) ----------


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:120]


@contextmanager
def _tick_lock(session_id: str, role: str = "formation"):
    """Non-blocking per-(session, role) file lock. Yields True if acquired, False
    if a tick for that (session, role) is already running."""
    lock_dir = LOG_PATH.parent / "locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        yield True  # can't lock → don't block the only tick
        return
    f = open(lock_dir / f"{_safe(session_id)}__{role}.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


@dataclass(slots=True)
class _Decision:
    """What a role decided to do with the current window."""

    skip: bool
    advance: bool = False        # only consulted when skip=True
    message: str | None = None   # the claude -p message when skip=False
    log: str | None = None


def _formation_decision(session_id: str, cwd: str, delta: str, n_lines: int,
                        flush: bool, armed: bool, watcher_session_id: str | None) -> _Decision:
    """Gate a formation window: a pure-chat turn with nothing armed isn't worth a
    model call (advance past it). Otherwise build the formation message."""
    if n_lines == 0:
        return _Decision(skip=True, advance=False)  # nothing new
    has_activity = ("TOOL (" in delta) or ("RESULT:" in delta)
    if not delta.strip() or (not flush and not armed and not has_activity):
        log = f"SKIP-GATE session={session_id} role=formation lines={n_lines}" if delta.strip() else None
        return _Decision(skip=True, advance=True, log=log)
    if watcher_session_id is None:
        message = build_watcher_prompt(cwd) + "\n\n--- Session activity ---\n\n" + delta
    else:
        message = WATCHER_SUBSEQUENT_HEADER + delta
    return _Decision(skip=False, message=message)


def _eval_decision(session_id: str, cwd: str, delta: str, n_lines: int,
                   flush: bool, armed: bool, watcher_session_id: str | None) -> _Decision:
    """Decide an eval window. Run only when surfaces are pending; with pending
    surfaces but no new evidence (and not a flush), DEFER (hold the cursor)."""
    with db.session() as conn:
        pending = session_state.pending_surfaces(conn, session_id)
    if not pending:
        # Nothing to judge — advance past this evidence so we don't re-read it.
        return _Decision(skip=True, advance=True)
    if n_lines == 0 and not flush:
        # Pending, but no forward evidence yet → defer (hold cursor).
        return _Decision(skip=True, advance=False)
    message = _eval_message(session_id, pending, delta, flush,
                            is_new=watcher_session_id is None)
    return _Decision(skip=False, message=message)


def _eval_message(session_id: str, pending_rows, delta: str, flush: bool,
                  is_new: bool) -> str:
    head = build_eval_prompt() if is_new else EVAL_SUBSEQUENT_HEADER
    lines = [
        head,
        f"SESSION_ID: {session_id}   (pass this verbatim to --session-id)",
        "",
        "PENDING SURFACES (judge each you can conclude):",
    ]
    for r in pending_rows:
        ft = r["first_token"] or "(path-glob)"
        lines.append(
            f'  [memory_id={r["memory_id"]}] "{r["name"]}" kind={r["kind"]} '
            f'surfaced_at_turn={r["turn_at_surface"]} first_token={ft}'
        )
        lines.append(f'      body: {(r["body"] or "")[:300]}')
    if flush:
        lines.append("")
        lines.append("THIS IS THE FINAL PASS: judge EVERY pending surface now; "
                     "default genuinely-inconclusive ones to `unused`.")
    lines += ["", "--- Forward activity ---", "",
              delta if delta.strip() else "(no new activity since the surface)"]
    return "\n".join(lines)


_DECIDERS = {"formation": _formation_decision, "eval": _eval_decision}


def _open_run(session_id: str, role: str, cwd: str, flush: bool,
              cursor_from: int) -> int | None:
    """Insert a 'running' watcher_runs row (autocommit `db.session` commits it
    immediately, before claude spawns) and return its id. Fail-open: the run log
    is monitoring — it must never break a tick."""
    try:
        with db.session() as conn:
            return runs_store.start_run(
                conn, work_session_id=session_id, role=role, pid=os.getpid(),
                started_ts=int(time.time()), model=_watcher_model(), flush=flush,
                cursor_from=cursor_from, cwd=cwd,
            )
    except Exception:
        return None


def _close_run(run_id: int | None, *, ok: bool, cursor_to: int,
               delta_chars: int, error: str | None) -> None:
    """Finalize the run row to ok/error. Fail-open."""
    if run_id is None:
        return
    try:
        with db.session() as conn:
            runs_store.finish_run(
                conn, run_id, status="ok" if ok else "error",
                ended_ts=int(time.time()), cursor_to=cursor_to,
                delta_chars=delta_chars, error=None if ok else (error or "")[:300],
            )
    except Exception:
        pass


def run_tick(session_id: str, transcript_path: str, cwd: str,
             role: str = "formation", flush: bool = False) -> int:
    """One event-driven tick for (session, role). See module docstring."""
    if not CLAUDE_BIN or not session_id or role not in _DECIDERS:
        return 0
    ensure_row(session_id, transcript_path, cwd, role)

    with _tick_lock(session_id, role) as got:
        if not got:
            _log(f"TICK-LOCKED session={session_id} role={role}")
            return 0

        # We hold the lock, so any prior 'running' run row for this (session,
        # role) belongs to a tick that died before finalizing — reap it.
        try:
            with db.session() as conn:
                runs_store.reap_stale(conn, session_id, role, int(time.time()))
        except Exception:
            pass

        st = state.read(session_id, role)
        last_line = st.last_line_read
        watcher_session_id = st.watcher_session_id
        fail_streak = st.fail_streak

        new_lines = _read_lines_from(transcript_path, last_line)
        delta = _format_delta(new_lines) if new_lines else ""

        decision = _DECIDERS[role](
            session_id, cwd, delta, len(new_lines), flush, st.armed, watcher_session_id,
        )
        if decision.skip:
            advance = len(new_lines) if decision.advance else 0
            state.commit_tick(session_id, role=role,
                              watcher_session_id=watcher_session_id,
                              last_line=last_line + advance, armed=0, fail_streak=0)
            if decision.log:
                _log(decision.log)
            return 0

        attempt = fail_streak + 1
        # Open the run row and commit it BEFORE spawning claude, so the model's
        # engram CLI calls can record events against it via $ENGRAM_RUN_ID.
        run_id = _open_run(session_id, role, cwd, flush, last_line)
        result = run_watcher_session(role, decision.message,
                                     resume=watcher_session_id, run_id=run_id)
        watcher_session_id = result.watcher_session_id
        failed = not result.ok
        _close_run(run_id, ok=not failed, cursor_to=last_line + len(new_lines),
                   delta_chars=len(delta), error=result.error)
        if failed:
            _log(f"MODEL-ERROR session={session_id} role={role} "
                 f"delta_chars={len(delta)} attempt={attempt} flush={int(flush)}")
        else:
            _log(f"MODEL-OK session={session_id} role={role} lines={len(new_lines)}")

        # Hold the window on failure, give up after the cap (fail_streak persisted).
        advance, fail_streak = _retry_decision(failed, fail_streak, MAX_FORM_RETRIES)
        if advance:
            if failed:
                _log(f"SKIP-GIVEUP session={session_id} role={role} "
                     f"lines={len(new_lines)} after {MAX_FORM_RETRIES} attempts")
            last_line += len(new_lines)
        elif watcher_session_id is not None:
            # Retry from a clean session so a bad turn in --resume history can't
            # bias the retry.
            watcher_session_id = None
        state.commit_tick(session_id, role=role, watcher_session_id=watcher_session_id,
                          last_line=last_line, armed=0, fail_streak=fail_streak)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: engram watcher-tick <session_id> <transcript_path> <cwd> [--flush] [--role formation|eval]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    flush = "--flush" in argv
    role = "formation"
    if "--role" in argv:
        i = argv.index("--role")
        if i + 1 < len(argv):
            role = argv[i + 1]
    pos = []
    skip_next = False
    for j, a in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if a == "--role":
            skip_next = True
            continue
        if a.startswith("--"):
            continue
        pos.append(a)
    if len(pos) < 3:
        print("Usage: engram watcher-tick <session_id> <transcript_path> <cwd> "
              "[--flush] [--role formation|eval]", file=sys.stderr)
        return 1
    try:
        return run_tick(pos[0], pos[1], pos[2], role=role, flush=flush)
    except Exception as e:  # pragma: no cover - tick must never crash loudly
        _log(f"TICK-CRASH session={pos[0]} role={role} error={e}")
        return 0
