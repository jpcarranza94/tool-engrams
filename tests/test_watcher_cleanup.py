"""Automatic cleanup of cold watcher residue (`engram cleanup`).

Three reapers, each best-effort and idempotent: dead watcher_state rows
(transcript gone), stale sandbox cwds in the temp dir, and the watcher
sessions' own old transcript dirs under ~/.claude/projects. SessionStart
spawns the cleanup detached, at most once per CLEANUP_INTERVAL_SEC.
"""

from __future__ import annotations

import os
import time

from toolengrams import db
from toolengrams.watcher import cleanup, state

OLD = time.time() - 30 * 86_400  # comfortably past any TTL


def _age(path, ts: float = OLD) -> None:
    os.utime(path, (ts, ts))


# ---------- prune_dead_sessions ----------


def _seed_row(session_id: str, transcript_path: str, *, ticked_ago: int) -> None:
    state.ensure_row(session_id, transcript_path, "/cwd")
    then = int(time.time()) - ticked_ago
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_tick_ts = ?, last_checked_ts = ?, "
            "created_ts = ? WHERE work_session_id = ?",
            (then, then, then, session_id),
        )


def _row_exists(session_id: str) -> bool:
    with db.session() as conn:
        return conn.execute(
            "SELECT 1 FROM watcher_state WHERE work_session_id = ?",
            (session_id,),
        ).fetchone() is not None


def test_prune_removes_old_rows_with_missing_transcript(temp_db, tmp_path):
    _seed_row("dead", str(tmp_path / "gone.jsonl"), ticked_ago=10 * 86_400)

    assert state.prune_dead_sessions(7 * 86_400) == 1
    assert not _row_exists("dead")


def test_prune_keeps_row_while_transcript_exists(temp_db, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    _seed_row("alive", str(transcript), ticked_ago=10 * 86_400)

    assert state.prune_dead_sessions(7 * 86_400) == 0
    assert _row_exists("alive")  # cursor preserved while the transcript lives


def test_prune_keeps_recent_row_even_without_transcript(temp_db, tmp_path):
    _seed_row("fresh", str(tmp_path / "gone.jsonl"), ticked_ago=60)

    assert state.prune_dead_sessions(7 * 86_400) == 0
    assert _row_exists("fresh")


# ---------- _reap_stale_dirs ----------


def test_reap_removes_only_our_old_dirs(tmp_path):
    ours_old = tmp_path / "engram-formation-aaa"
    ours_new = tmp_path / "engram-eval-bbb"
    theirs_old = tmp_path / "some-user-project"
    for d in (ours_old, ours_new, theirs_old):
        d.mkdir()
    _age(ours_old)
    _age(theirs_old)

    is_ours = lambda name: name.startswith(cleanup._REAP_PREFIXES)
    removed = cleanup._reap_stale_dirs(tmp_path, time.time() - 7 * 86_400, is_ours)

    assert removed == 1
    assert not ours_old.exists()
    assert ours_new.exists()        # too fresh
    assert theirs_old.exists()      # not ours, regardless of age


def test_reap_judges_staleness_by_newest_content(tmp_path):
    """Overwriting a file does NOT bump the parent dir's mtime, so a live
    sandbox can look cold by dir-stat alone. The newest direct child decides."""
    sandbox = tmp_path / "engram-formation-ccc"
    sandbox.mkdir()
    delta = sandbox / "delta.txt"
    delta.write_text("recent tick")
    _age(sandbox)  # dir mtime old, content fresh

    removed = cleanup._reap_stale_dirs(
        tmp_path, time.time() - 7 * 86_400,
        lambda name: name.startswith(cleanup._REAP_PREFIXES))

    assert removed == 0
    assert sandbox.exists()


def test_reap_never_follows_symlinks(tmp_path):
    target = tmp_path / "victim"
    target.mkdir()
    link = tmp_path / "engram-formation-ddd"
    link.symlink_to(target)
    _age(link)

    cleanup._reap_stale_dirs(tmp_path, time.time(),
                             lambda name: name.startswith("engram-"))

    assert target.exists()
    assert link.exists()  # skipped, not reaped


# ---------- maybe_spawn_cleanup ----------


def test_spawn_gated_by_fresh_marker(tmp_path, monkeypatch):
    marker = tmp_path / "last-cleanup"
    marker.touch()
    monkeypatch.setattr(cleanup, "_marker_path", lambda: marker)
    spawned = []
    monkeypatch.setattr(cleanup.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a))

    assert cleanup.maybe_spawn_cleanup() is False
    assert spawned == []


def test_spawn_fires_once_when_marker_stale_then_regates(tmp_path, monkeypatch):
    marker = tmp_path / "last-cleanup"
    marker.touch()
    _age(marker)
    monkeypatch.setattr(cleanup, "_marker_path", lambda: marker)
    spawned = []
    monkeypatch.setattr(cleanup.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a))

    assert cleanup.maybe_spawn_cleanup() is True
    assert len(spawned) == 1
    # The marker was touched before spawning, so the next call is gated.
    assert cleanup.maybe_spawn_cleanup() is False
    assert len(spawned) == 1
