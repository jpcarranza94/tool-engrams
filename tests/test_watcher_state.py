"""Tests for the watcher_state persistence seam (watcher/state.py)."""

from __future__ import annotations

import time

from toolengrams import db
from toolengrams.target import claude_code as target_claude
from toolengrams.watcher import state


def _set(session_id: str, **cols) -> None:
    """Directly poke watcher_state columns for a tracked session."""
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db.session() as conn:
        conn.execute(
            f"UPDATE watcher_state SET {sets} WHERE work_session_id = ?",
            (*cols.values(), session_id),
        )


# ---------- ensure_row / read / commit_tick ----------


def test_ensure_row_is_idempotent(temp_db):
    state.ensure_row("s", "/t.jsonl", "/cwd")
    state.ensure_row("s", "/other.jsonl", "/cwd")  # second call must not clobber
    row = temp_db.execute(
        "SELECT transcript_path, last_line_read FROM watcher_state WHERE work_session_id = 's'"
    ).fetchone()
    assert row["transcript_path"] == "/t.jsonl"   # first write wins (INSERT OR IGNORE)
    assert row["last_line_read"] == 0


def test_read_missing_row_returns_fresh_state(temp_db):
    st = state.read("nope")
    assert st == state.TickState(last_line_read=0, armed=False, fail_streak=0)


def test_commit_tick_roundtrips_and_bumps_last_tick(temp_db):
    state.ensure_row("s", "/t.jsonl", "/cwd")
    state.commit_tick("s", last_line=7, armed=1, fail_streak=2)

    st = state.read("s")
    assert st.last_line_read == 7
    assert st.armed is True
    assert st.fail_streak == 2
    # last_tick_ts was bumped off its 0 default.
    assert temp_db.execute(
        "SELECT last_tick_ts FROM watcher_state WHERE work_session_id = 's'"
    ).fetchone()["last_tick_ts"] > 0


# ---------- arm ----------


def test_arm_sets_flag(temp_db):
    state.ensure_row("s", "/t.jsonl", "/cwd")
    state.arm("s")
    assert state.read("s").armed is True


# ---------- seconds_since_tick ----------


def test_seconds_since_tick_never_ticked_is_huge(temp_db):
    state.ensure_row("s", "/t.jsonl", "/cwd")  # last_tick_ts = 0
    assert state.seconds_since_tick("s") >= state._NEVER


def test_seconds_since_tick_recent_is_small(temp_db):
    state.ensure_row("s", "/t.jsonl", "/cwd")
    _set("s", last_tick_ts=int(time.time()) - 5)
    assert 0 <= state.seconds_since_tick("s") < 60


# ---------- sweep_idle ----------


def _track_with_tail(tmp_path, session_id, *, n_lines, cursor, tick_age_sec):
    """Create a tracked session whose transcript has n_lines, cursor at `cursor`,
    and a last tick `tick_age_sec` in the past."""
    f = tmp_path / f"{session_id}.jsonl"
    f.write_text("".join(f'{{"i": {i}}}\n' for i in range(n_lines)))
    state.ensure_row(session_id, str(f), "/cwd")
    _set(session_id, last_line_read=cursor, last_tick_ts=int(time.time()) - tick_age_sec)
    return str(f)


def test_sweep_idle_returns_session_with_unread_tail(temp_db, tmp_path):
    _track_with_tail(tmp_path, "idle", n_lines=10, cursor=4, tick_age_sec=3600)
    idle = state.sweep_idle(idle_sec=1800)
    assert [s.session_id for s in idle] == ["idle"]
    assert idle[0].cwd == "/cwd"


def test_sweep_idle_excludes_current_session(temp_db, tmp_path):
    _track_with_tail(tmp_path, "idle", n_lines=10, cursor=4, tick_age_sec=3600)
    assert state.sweep_idle(idle_sec=1800, exclude_session_id="idle") == []


def test_sweep_idle_excludes_recent_tick(temp_db, tmp_path):
    _track_with_tail(tmp_path, "fresh", n_lines=10, cursor=4, tick_age_sec=10)
    assert state.sweep_idle(idle_sec=1800) == []


def test_sweep_idle_excludes_never_ticked(temp_db, tmp_path):
    # last_tick_ts stays 0 (never ran a tick) → no completed turn → no tail.
    f = tmp_path / "new.jsonl"
    f.write_text('{"i": 0}\n{"i": 1}\n')
    state.ensure_row("new", str(f), "/cwd")
    _set("new", last_line_read=0)  # leaves last_tick_ts at its 0 default
    assert state.sweep_idle(idle_sec=1800) == []


def test_sweep_idle_excludes_fully_read_transcript(temp_db, tmp_path):
    # cursor at EOF → nothing unread → not a lost tail.
    _track_with_tail(tmp_path, "done", n_lines=5, cursor=5, tick_age_sec=3600)
    assert state.sweep_idle(idle_sec=1800) == []


def test_sweep_idle_is_bounded_by_limit(temp_db, tmp_path):
    # More idle sessions than the limit → only `limit` returned, oldest first.
    for i in range(5):
        _track_with_tail(tmp_path, f"idle-{i}", n_lines=3, cursor=0,
                         tick_age_sec=3600 + i * 100)  # idle-4 is oldest
    got = state.sweep_idle(idle_sec=1800, limit=2)
    assert len(got) == 2
    assert {s.session_id for s in got} == {"idle-4", "idle-3"}  # two oldest ticks


# ---------- _has_unread_lines (EOF boundary) ----------


def test_has_unread_lines_boundaries(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("a\nb\nc\nd\ne\n")  # 5 lines
    assert state._has_unread_lines(str(f), 5) is False  # cursor == EOF
    assert state._has_unread_lines(str(f), 4) is True   # one line past cursor
    assert state._has_unread_lines(str(f), 0) is True   # nothing read yet
    assert state._has_unread_lines(str(f), 6) is False  # cursor past EOF
    assert state._has_unread_lines("/nonexistent.jsonl", 0) is False


# ---------- derive_transcript_path ----------


def test_derive_transcript_path_shape():
    p = target_claude.derive_transcript_path("sess-1", "/Users/x/proj")
    assert p.endswith("/.claude/projects/-Users-x-proj/sess-1.jsonl")
