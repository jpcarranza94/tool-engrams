"""watcher_state persistence — the single seam over the watcher_state table.

Every read/write of a session's tick cursor and retry/arm state goes through
here. The event-driven tick (`tick.py`) and the SessionStart idle-sweep are the
only callers; no raw `watcher_state` SQL lives outside this module.

A session has TWO watcher roles — `formation` (creates memories) and
`eval` (judges surfaced memories) — each an independent watcher session with its
own cursor / resume id / retry streak. `watcher_state` is keyed
`(work_session_id, role)`, so every accessor takes a `role` (default
`'formation'`, which keeps the formation callers untouched).

The shape a tick reads and commits is `TickState`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..utils import slugify_cwd
from .log import _log

# `seconds_since_tick` returns this when a session has never ticked (or on
# error) so the coalesce gate always lets the next tick through (fail-open:
# better an extra tick than a missed one). ~31 years in seconds — unreachable
# as a real elapsed time, so it can't be mistaken for one.
_NEVER = 10 ** 9

# Hard cap on how many abandoned sessions one idle-sweep recovers. watcher_state
# rows are not GC'd, so without a bound a long-lived install could stat-and-fire
# for an unbounded backlog at a single SessionStart. Oldest ticks first.
DEFAULT_SWEEP_LIMIT = 50


@dataclass
class TickState:
    """The per-session-per-role state one tick reads and commits."""

    last_line_read: int
    watcher_session_id: str | None
    armed: bool
    fail_streak: int


@dataclass
class IdleSession:
    """A tracked session with unread transcript lines and an old last tick —
    a candidate for a flush tick (tail recovery)."""

    session_id: str
    transcript_path: str
    cwd: str


def derive_transcript_path(session_id: str, cwd: str) -> str:
    """Derive the JSONL transcript path from session_id and cwd."""
    slug = slugify_cwd(cwd)
    return str(Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl")


def ensure_row(session_id: str, transcript_path: str, cwd: str,
               role: str = "formation") -> None:
    """Create the watcher_state row for (session, role) if missing (idempotent).
    Called by SessionStart and defensively by the tick so ticks are
    self-sufficient."""
    try:
        now_ts = int(time.time())
        with db.session() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watcher_state "
                "(work_session_id, role, transcript_path, last_line_read, "
                " last_checked_ts, cwd, created_ts) VALUES (?, ?, ?, 0, ?, ?, ?)",
                (session_id, role, transcript_path, now_ts, cwd, now_ts),
            )
    except Exception:
        pass


def read(session_id: str, role: str = "formation") -> TickState:
    """Read the tick state for (session, role). Missing row → fresh zero state."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT last_line_read, watcher_session_id, armed, fail_streak "
            "FROM watcher_state WHERE work_session_id = ? AND role = ?",
            (session_id, role),
        ).fetchone()
    if row is None:
        return TickState(last_line_read=0, watcher_session_id=None,
                         armed=False, fail_streak=0)
    return TickState(
        last_line_read=row["last_line_read"],
        watcher_session_id=row["watcher_session_id"],
        armed=bool(row["armed"]),
        fail_streak=row["fail_streak"],
    )


def commit_tick(session_id: str, *, watcher_session_id: str | None,
                last_line: int, armed: int, fail_streak: int,
                role: str = "formation") -> None:
    """Persist the outcome of one tick: cursor + retry/arm state + timestamps.

    Bumps `last_tick_ts` (and `last_checked_ts`) unconditionally so the coalesce
    gate and the idle-sweep see the tick happened, even on a no-op window."""
    now_ts = int(time.time())
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET watcher_session_id = ?, last_line_read = ?, "
            "armed = ?, fail_streak = ?, last_tick_ts = ?, last_checked_ts = ? "
            "WHERE work_session_id = ? AND role = ?",
            (watcher_session_id, last_line, armed, fail_streak, now_ts, now_ts,
             session_id, role),
        )


def arm(session_id: str, role: str = "formation") -> None:
    """Mark a session armed (a tool failure happened): the next turn-boundary
    formation tick runs the model even if that turn had no tool lines.

    arm is the highest-value formation signal, so a write failure is logged
    rather than swallowed silently."""
    try:
        with db.session() as conn:
            conn.execute(
                "UPDATE watcher_state SET armed = 1 "
                "WHERE work_session_id = ? AND role = ?",
                (session_id, role),
            )
    except Exception as e:
        _log(f"ARM-ERROR session={session_id} error={e}")


def seconds_since_tick(session_id: str, role: str = "formation") -> int:
    """Seconds since this (session, role)'s last tick. Returns a large sentinel
    if the session never ticked or on any error — so the coalesce gate (policy
    lives in tick.py) always lets the next tick through."""
    try:
        now_ts = int(time.time())
        with db.session() as conn:
            row = conn.execute(
                "SELECT last_tick_ts FROM watcher_state "
                "WHERE work_session_id = ? AND role = ?",
                (session_id, role),
            ).fetchone()
        last = (row["last_tick_ts"] if row else 0) or 0
        return now_ts - last if last > 0 else _NEVER
    except Exception:
        return _NEVER


def sweep_idle(idle_sec: int, exclude_session_id: str = "",
               limit: int = DEFAULT_SWEEP_LIMIT) -> list[IdleSession]:
    """Tracked FORMATION sessions that ticked at least once, whose transcript
    still has unread lines, and whose last tick is older than `idle_sec`. At most
    `limit` rows (oldest tick first) are considered per call.

    This is the backstop for a tail lost when a session died (hard kill, crash,
    OOM) before its final Stop/flush fired. Scoped to the formation row — the
    SessionStart sweep re-fires a formation flush, then opportunistically an eval
    flush (which self-gates on pending surfaces). A `last_tick_ts` of 0 means the
    session never produced a completed turn, so there is no tail to recover."""
    out: list[IdleSession] = []
    try:
        cutoff = int(time.time()) - idle_sec
        with db.session() as conn:
            rows = conn.execute(
                "SELECT work_session_id, transcript_path, cwd, last_line_read "
                "FROM watcher_state "
                "WHERE role = 'formation' AND last_tick_ts > 0 AND last_tick_ts < ? "
                "  AND transcript_path != '' AND work_session_id != ? "
                "ORDER BY last_tick_ts ASC LIMIT ?",
                (cutoff, exclude_session_id, limit),
            ).fetchall()
        for row in rows:
            if _has_unread_lines(row["transcript_path"], row["last_line_read"]):
                out.append(IdleSession(
                    session_id=row["work_session_id"],
                    transcript_path=row["transcript_path"],
                    cwd=row["cwd"] or "",
                ))
    except Exception as e:
        _log(f"SWEEP-ERROR error={e}")
    return out


def _has_unread_lines(transcript_path: str, cursor: int) -> bool:
    """True if the transcript has at least one line past `cursor`. Short-circuits
    on the first unread line so it never reads a whole transcript."""
    try:
        with open(transcript_path) as f:
            for i, _ in enumerate(f):
                if i >= cursor:
                    return True
        return False
    except OSError:
        return False
