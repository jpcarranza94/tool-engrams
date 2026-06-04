"""Event-driven watcher tick.

Replaces the 5-minute cron poll: hooks fire a single detached `engram
watcher-tick` per meaningful event (a completed turn, a session end, a
detected failure→success, a user correction). One tick = read the transcript
delta since the cursor → gate → call the watcher model → save → advance.

State that the old in-process loop kept in local variables now lives in
`watcher_state` (cursor / armed / fail_streak / last_tick_ts), behind the
`state` module, so it survives across the independent per-event tick processes.

Concurrency: ticks for the same session are serialized by a non-blocking file
lock. If a tick is already running, a newly-fired one exits immediately — the
in-flight tick reads to the current EOF, and the next event re-triggers if more
arrived. This is what prevents two ticks racing the cursor or double-resuming
the same `claude` session.

Tail recovery: a session can die (hard kill, crash) before its final Stop/flush
fires, leaving the last window unprocessed. `sweep_idle_sessions` (run from
SessionStart) re-fires a flush tick for any tracked session with unread lines
and an old last tick.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from ..prompts.watcher import WATCHER_SUBSEQUENT_HEADER
from ..utils import WATCHER_CHILD_ENV
from . import state
from .agent import (
    CLAUDE_BIN,
    WATCHER_SCHEMA,
    _build_initial_prompt,
    _claude_p_new,
    _claude_p_resume,
    _extract_session_id,
    _parse_response,
    _save_memory,
)
from .log import LOG_PATH, _log
from .state import ensure_row
from .transcript_format import _format_delta, _read_lines_from

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = sys.executable

# How many consecutive failed attempts on the SAME transcript window before we
# give up and advance past it. A failure (model exception/timeout, or an
# unparseable response) HOLDS the cursor and retries next tick — recovering
# transient failures (529 overload, a one-off timeout, empty stdout). The cap
# stops a genuinely poison window from wedging the watcher. `fail_streak` is
# persisted in watcher_state, so the retry count carries across tick processes.
MAX_FORM_RETRIES = 3

# Minimum seconds between ticks for one session. A burst of triggers (rapid
# turns, a failure + the next Stop) coalesces into a single model call over the
# accumulated delta. This is a debounce, NOT a poll: no events → no tick. Flush
# triggers (session end / compaction) ignore it. Tunable via env.
DEFAULT_TICK_COALESCE_SEC = 45

# A tracked session whose last tick is older than this (and which still has
# unread lines) is treated as abandoned, and its tail is recovered by a flush
# tick at the next SessionStart. Tunable via env.
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
    """Mark the session 'armed' (a tool failure happened). The next turn-
    boundary tick will run the model even if that turn had no tool_use lines,
    so an error→fix episode is never gated out. Ensures the row first so an
    arm before any SessionStart tracking still sticks."""
    if transcript_path:
        ensure_row(session_id, transcript_path, cwd)
    state.arm(session_id)


def should_spawn(session_id: str, flush: bool) -> bool:
    """Coalesce gate (hook side): skip spawning a tick if one ran very recently,
    unless this is a flush. The policy lives here; the data (seconds since the
    last tick) comes from the state store."""
    if flush:
        return True
    return state.seconds_since_tick(session_id) >= _coalesce_sec()


def spawn_tick(session_id: str, transcript_path: str, cwd: str, flush: bool = False) -> None:
    """Fire-and-forget a detached `engram watcher-tick`. Returns immediately so
    the hook never blocks the user. ENGRAM_IN_WATCHER guards against the tick's
    own `claude` recursively triggering more ticks."""
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env[WATCHER_CHILD_ENV] = "1"
        argv = [PYTHON_BIN, "-m", "toolengrams", "watcher-tick",
                session_id, transcript_path, cwd]
        if flush:
            argv.append("--flush")
        subprocess.Popen(
            argv, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _log(f"TICK-SPAWN-ERROR session={session_id} error={e}")


def trigger(session_id: str, transcript_path: str, cwd: str,
            reason: str, flush: bool = False) -> None:
    """Convenience for hooks: coalesce-gate then spawn a detached tick."""
    if not session_id or not transcript_path:
        return
    if should_spawn(session_id, flush):
        spawn_tick(session_id, transcript_path, cwd, flush=flush)
    else:
        _log(f"TICK-COALESCED session={session_id} reason={reason}")


def sweep_idle_sessions(current_session_id: str) -> int:
    """Backstop for lost tails: re-fire a flush tick for every tracked session
    (other than the current one) with unread lines and an old last tick. Run
    from SessionStart. Returns the number of sessions re-triggered."""
    idle = state.sweep_idle(_idle_sweep_sec(), exclude_session_id=current_session_id)
    for s in idle:
        spawn_tick(s.session_id, s.transcript_path, s.cwd, flush=True)
    if idle:
        _log(f"IDLE-SWEEP recovered={len(idle)} from_session={current_session_id}")
    return len(idle)


# ---------- tick body (runs in the detached process) ----------


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:120]


@contextmanager
def _tick_lock(session_id: str):
    """Non-blocking per-session file lock. Yields True if acquired, False if a
    tick is already running for this session."""
    lock_dir = LOG_PATH.parent / "locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        yield True  # can't lock → don't block the only tick
        return
    f = open(lock_dir / f"{_safe(session_id)}.lock", "w")
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


def run_tick(session_id: str, transcript_path: str, cwd: str, flush: bool = False) -> int:
    """One event-driven tick. See module docstring."""
    if not CLAUDE_BIN or not session_id:
        return 0
    ensure_row(session_id, transcript_path, cwd)

    with _tick_lock(session_id) as got:
        if not got:
            _log(f"TICK-LOCKED session={session_id}")  # another tick covers the delta
            return 0

        st = state.read(session_id)
        last_line = st.last_line_read
        watcher_session_id = st.watcher_session_id
        armed = st.armed
        fail_streak = st.fail_streak

        new_lines = _read_lines_from(transcript_path, last_line)
        if not new_lines:
            state.commit_tick(session_id, watcher_session_id=watcher_session_id,
                              last_line=last_line, armed=0, fail_streak=fail_streak)
            return 0

        delta = _format_delta(new_lines)
        has_activity = ("TOOL (" in delta) or ("RESULT:" in delta)

        # GATE: a pure-chat turn with nothing armed isn't worth a model call.
        # Advance past it (we won't reprocess chat) and clear state.
        if not delta.strip() or (not flush and not armed and not has_activity):
            state.commit_tick(session_id, watcher_session_id=watcher_session_id,
                              last_line=last_line + len(new_lines), armed=0, fail_streak=0)
            if delta.strip():
                _log(f"SKIP-GATE session={session_id} lines={len(new_lines)}")
            return 0

        failed = False
        attempt = fail_streak + 1
        try:
            if watcher_session_id is None:
                message = _build_initial_prompt(cwd) + delta
                stdout = _claude_p_new(message, WATCHER_SCHEMA)
                watcher_session_id = _extract_session_id(stdout)
            else:
                message = WATCHER_SUBSEQUENT_HEADER + delta
                stdout = _claude_p_resume(watcher_session_id, message, WATCHER_SCHEMA)
        except Exception as e:
            _log(f"MODEL-ERROR session={session_id} delta_chars={len(delta)} "
                 f"attempt={attempt} flush={int(flush)} error={e}")
            failed = True

        if not failed:
            response = _parse_response(stdout)
            action = (response or {}).get("action") or "parse_error"
            if action == "create":
                for mem in response.get("memories", []):
                    try:
                        _save_memory(mem, cwd)
                        _log(f"SAVE session={session_id} name={mem.get('name', '?')}")
                    except Exception as e:
                        _log(f"SAVE-ERROR session={session_id} error={e}")
            elif action == "parse_error":
                preview = (stdout or "")[:300].replace("\n", "\\n")
                _log(f"MODEL-PARSE_ERROR session={session_id} attempt={attempt} "
                     f"lines={len(new_lines)} stdout={preview}")
                failed = True
            else:
                _log(f"MODEL-{action.upper()} session={session_id} lines={len(new_lines)}")

        # Same retry semantics as the old loop, but fail_streak is now persisted
        # across events: hold the window on failure, give up after the cap.
        advance, fail_streak = _retry_decision(failed, fail_streak, MAX_FORM_RETRIES)
        if advance:
            if failed:
                _log(f"SKIP-GIVEUP session={session_id} lines={len(new_lines)} "
                     f"after {MAX_FORM_RETRIES} attempts")
            last_line += len(new_lines)
        elif watcher_session_id is not None:
            # Retry from a clean session so a bad turn already in --resume
            # history can't bias the retry.
            watcher_session_id = None
        # armed is consumed once we've run a model interaction for this window.
        state.commit_tick(session_id, watcher_session_id=watcher_session_id,
                          last_line=last_line, armed=0, fail_streak=fail_streak)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: engram watcher-tick <session_id> <transcript_path> <cwd> [--flush]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    flush = "--flush" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) < 3:
        print("Usage: engram watcher-tick <session_id> <transcript_path> <cwd> [--flush]",
              file=sys.stderr)
        return 1
    try:
        return run_tick(pos[0], pos[1], pos[2], flush=flush)
    except Exception as e:  # pragma: no cover - tick must never crash loudly
        _log(f"TICK-CRASH session={pos[0]} error={e}")
        return 0
