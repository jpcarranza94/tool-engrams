"""Watcher process lifecycle + cron loop + DB cursor management.

This is the long-running side of the watcher: spawning the detached
subprocess, persisting its progress to `watcher_state`, the per-tick read /
delta-format / model-call / save loop, and graceful SIGTERM cleanup.

Pure transcript parsing lives in transcript_format.py; the model invocation
lives in agent.py. This module wires them to the DB.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import db
from ..prompts.watcher import WATCHER_SUBSEQUENT_HEADER
from ..utils import WATCHER_CHILD_ENV, slugify_cwd
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
from .transcript_format import (
    DEFAULT_SESSION_TIMEOUT_MIN,
    _format_delta,
    _is_session_alive,
    _read_lines_from,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = Path.home() / ".claude" / "tool-engrams" / "watcher.log"
PYTHON_BIN = sys.executable

WATCHER_INTERVAL = 300  # 5 minutes
SESSION_TIMEOUT = DEFAULT_SESSION_TIMEOUT_MIN  # minutes of inactivity before exit

# How many consecutive failed attempts on the SAME transcript window before we
# give up and advance past it. A failure (model exception/timeout, or an
# unparseable response) used to advance the cursor immediately, silently
# dropping that window forever. Instead we HOLD the cursor and retry next tick
# — recovering transient failures (529 overload, a one-off timeout, empty
# stdout). The cap stops a genuinely poison window from wedging the watcher.
MAX_FORM_RETRIES = 3


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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `engram watcher`."""
    if not argv or len(argv) < 3:
        print("Usage: engram watcher <session_id> <transcript_path> <cwd>", file=sys.stderr)
        return 1
    session_id, transcript_path, cwd = argv[0], argv[1], argv[2]
    return watcher_main(session_id, transcript_path, cwd)


def watcher_main(session_id: str, transcript_path: str, cwd: str) -> int:
    """Long-running cron: wake every 5 min, read delta, call the watcher model."""
    _log(f"START session={session_id} transcript={transcript_path}")

    # Handle SIGTERM gracefully. signum/frame required by signal API but unused.
    def _handle_sigterm(_signum, _frame):
        _log(f"SIGTERM session={session_id}")
        _cleanup(session_id)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Resume from where we left off if this is a respawn (watcher_state
    # persists last_line_read across restarts). Fresh sessions start at 0.
    watcher_session_id = None
    last_line = _get_saved_cursor(session_id)
    fail_streak = 0  # consecutive failed attempts on the current window

    try:
        while True:
            time.sleep(WATCHER_INTERVAL)

            # Liveness: exit if transcript hasn't been touched in 30 min.
            if not _is_session_alive(transcript_path, SESSION_TIMEOUT):
                _log(f"TIMEOUT session={session_id} (no activity for {SESSION_TIMEOUT} min)")
                break

            # Read new lines.
            new_lines = _read_lines_from(transcript_path, last_line)
            if not new_lines:
                _update_state(session_id, watcher_session_id, last_line)
                continue

            delta = _format_delta(new_lines)
            if not delta.strip():
                last_line += len(new_lines)
                _update_state(session_id, watcher_session_id, last_line)
                continue

            if not CLAUDE_BIN:
                last_line += len(new_lines)
                continue

            # Call the watcher model.
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
                _log(
                    f"MODEL-ERROR session={session_id} "
                    f"delta_chars={len(delta)} attempt={attempt} error={e}"
                )
                failed = True

            # Parse + save (only if the call itself succeeded).
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
                    # Log enough of the raw stdout to diagnose what went wrong.
                    stdout_preview = (stdout or "")[:300].replace("\n", "\\n")
                    _log(
                        f"MODEL-PARSE_ERROR session={session_id} attempt={attempt} "
                        f"lines={len(new_lines)} stdout={stdout_preview}"
                    )
                    failed = True
                else:
                    _log(f"MODEL-{action.upper()} session={session_id} lines={len(new_lines)}")

            # Cursor-advance decision. On failure we HOLD the cursor and retry
            # the same window next tick (recovers transient 529 / timeout /
            # empty stdout); after MAX_FORM_RETRIES we give up and advance past
            # it so a poison window can't wedge the watcher forever.
            advance, fail_streak = _retry_decision(failed, fail_streak, MAX_FORM_RETRIES)
            if advance:
                if failed:
                    _log(
                        f"SKIP-GIVEUP session={session_id} lines={len(new_lines)} "
                        f"after {MAX_FORM_RETRIES} attempts"
                    )
                last_line += len(new_lines)
            elif watcher_session_id is not None:
                # Holding this window for a retry: restart from a CLEAN claude
                # session. A parse failure means a bad turn (e.g. a
                # conversational reply that didn't match the schema) is already
                # in this session's --resume history; re-feeding the same delta
                # into it would bias the retry toward repeating the mistake.
                # Dropping the id forces the next attempt through _claude_p_new.
                watcher_session_id = None
            # _update_state runs UNCONDITIONALLY (not only when advancing) so
            # last_checked_ts keeps bumping during a retry hold — otherwise a
            # held window would look like a stalled watcher to the user_prompt
            # zombie check and get killed mid-retry.
            _update_state(session_id, watcher_session_id, last_line)
    except Exception as e:
        _log(f"CRASH session={session_id} error={e}")
    finally:
        _cleanup(session_id)

    _log(f"EXIT session={session_id}")
    return 0


def spawn_watcher(session_id: str, transcript_path: str, cwd: str) -> None:
    """Spawn watcher as a detached background process and record state.

    On respawn (watcher_state row already exists), preserves last_line_read
    so the new watcher resumes from where the old one left off — avoids
    re-reading the entire transcript and creating duplicate memories.
    """
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        # Mark this subprocess (and any `claude` it launches) as watcher-owned
        # so its hooks refuse to recursively spawn another watcher.
        env[WATCHER_CHILD_ENV] = "1"

        proc = subprocess.Popen(
            [PYTHON_BIN, "-m", "toolengrams", "watcher",
             session_id, transcript_path, cwd],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        now_ts = int(time.time())
        with db.session() as conn:
            # Use INSERT ... ON CONFLICT to preserve last_line_read on respawn.
            # Fresh sessions get last_line_read=0; respawns keep their cursor.
            conn.execute(
                "INSERT INTO watcher_state "
                "(work_session_id, watcher_pid, transcript_path, "
                " last_line_read, last_checked_ts, cwd, created_ts) "
                "VALUES (?, ?, ?, 0, ?, ?, ?) "
                "ON CONFLICT(work_session_id) DO UPDATE SET "
                "watcher_pid = excluded.watcher_pid, "
                "transcript_path = excluded.transcript_path, "
                "last_checked_ts = excluded.last_checked_ts, "
                "cwd = excluded.cwd",
                (session_id, proc.pid, transcript_path, now_ts, cwd, now_ts),
            )

        _log(f"SPAWN session={session_id} pid={proc.pid}")
    except Exception as e:
        _log(f"SPAWN-ERROR session={session_id} error={e}")


def derive_transcript_path(session_id: str, cwd: str) -> str:
    """Derive the JSONL transcript path from session_id and cwd."""
    slug = slugify_cwd(cwd)
    return str(Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl")


# ---------- internal: DB cursor + log ----------


def _get_saved_cursor(session_id: str) -> int:
    """Read the last_line_read cursor from watcher_state.

    Returns 0 if no state exists (fresh session). On respawn, returns the
    cursor from the previous watcher instance so we don't re-read the
    entire transcript.
    """
    try:
        with db.session() as conn:
            row = conn.execute(
                "SELECT last_line_read FROM watcher_state WHERE work_session_id = ?",
                (session_id,),
            ).fetchone()
        return row["last_line_read"] if row else 0
    except Exception:
        return 0


def _update_state(
    session_id: str,
    watcher_session_id: str | None,
    last_line: int,
) -> None:
    """Update watcher_state table with current progress."""
    try:
        now_ts = int(time.time())
        with db.session() as conn:
            conn.execute(
                "UPDATE watcher_state SET "
                "watcher_session_id = ?, last_line_read = ?, last_checked_ts = ? "
                "WHERE work_session_id = ?",
                (watcher_session_id, last_line, now_ts, session_id),
            )
    except Exception:
        pass


def _cleanup(session_id: str) -> None:
    """Mark watcher as inactive but PRESERVE cursor for respawn.

    Clears watcher_pid and watcher_session_id so the liveness check
    knows the watcher is dead, but keeps last_line_read so a respawned
    watcher resumes from where we left off instead of re-reading the
    entire transcript.
    """
    try:
        with db.session() as conn:
            conn.execute(
                "UPDATE watcher_state SET watcher_pid = NULL, "
                "watcher_session_id = NULL WHERE work_session_id = ?",
                (session_id,),
            )
    except Exception:
        pass


def _log(msg: str) -> None:
    """Append a line to the watcher log."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
