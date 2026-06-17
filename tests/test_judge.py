"""`engram judge <memory_id> <outcome>` — the evaluation watcher's one verb.

Sets session_surfaces.outcome AND bumps the memory counter in one transaction:
  helpful → useful_count++   unused → (neither)   noise → noise_count++
Validation boundary: unknown id, id-not-in-session, bad outcome, no session.
Idempotent: only writes outcome IS NULL, so a retry is a noop.

CLI mains open their own db.session() against $ENGRAM_DB (set by temp_db).
"""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import judge
from toolengrams.retrieval import session_state


def _mem(conn, name="m", kind="hint") -> int:
    return memory_store.insert_memory(
        conn, name=name, description="", body="b", kind=kind,
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )


def _surface(conn, session_id, memory_id, *, ts=None, hook="pre_tool_use"):
    session_state.log_surfaces(
        conn, session_id, [memory_id], "tu-1", hook, 1, ts or int(time.time()),
    )


def _counts(conn, memory_id) -> tuple[int, int]:
    row = conn.execute(
        "SELECT useful_count, noise_count FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    return row["useful_count"], row["noise_count"]


def _outcomes(conn, session_id, memory_id) -> list:
    return [
        r["outcome"]
        for r in conn.execute(
            "SELECT outcome FROM session_surfaces WHERE session_id = ? AND memory_id = ? "
            "ORDER BY surfaced_ts",
            (session_id, memory_id),
        ).fetchall()
    ]


def test_helpful_bumps_useful_and_marks_surface(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid)

    rc = judge.main([str(mid), "helpful", "--session-id", "sess-1"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "judged"
    assert _counts(temp_db, mid) == (1, 0)
    assert _outcomes(temp_db, "sess-1", mid) == ["helpful"]


def test_noise_bumps_noise_and_marks_surface(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid)

    rc = judge.main([str(mid), "noise", "--session-id", "sess-1"])
    assert rc == 0
    assert _counts(temp_db, mid) == (0, 1)
    assert _outcomes(temp_db, "sess-1", mid) == ["noise"]


def test_unused_marks_surface_bumps_neither(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid)

    rc = judge.main([str(mid), "unused", "--session-id", "sess-1"])
    assert rc == 0
    assert _counts(temp_db, mid) == (0, 0)
    assert _outcomes(temp_db, "sess-1", mid) == ["unused"]


def test_judge_is_idempotent(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid)

    assert judge.main([str(mid), "helpful", "--session-id", "sess-1"]) == 0
    capsys.readouterr()
    rc = judge.main([str(mid), "helpful", "--session-id", "sess-1"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "noop"
    assert _counts(temp_db, mid) == (1, 0)  # not double-bumped


def test_marks_all_pending_surfaces_and_bumps_per_row(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid, ts=1000)
    _surface(temp_db, "sess-1", mid, ts=2000)

    rc = judge.main([str(mid), "helpful", "--session-id", "sess-1"])
    assert rc == 0
    assert _outcomes(temp_db, "sess-1", mid) == ["helpful", "helpful"]
    # Counter tracks helpful SURFACES (rows closed), so the q inputs stay
    # consistent with session_surfaces and `rebuild-counters`.
    assert _counts(temp_db, mid) == (2, 0)


def test_unknown_memory_id_rejected(temp_db, capsys):
    rc = judge.main(["999", "helpful", "--session-id", "sess-1"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_found"


def test_memory_not_surfaced_in_session_rejected(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "other-sess", mid)  # surfaced elsewhere, not in sess-1
    rc = judge.main([str(mid), "helpful", "--session-id", "sess-1"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_in_session"


def test_bad_outcome_rejected(temp_db, capsys):
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid)
    rc = judge.main([str(mid), "bogus", "--session-id", "sess-1"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_outcome"


def test_no_session_rejected(temp_db, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    mid = _mem(temp_db)
    _surface(temp_db, "sess-1", mid)
    rc = judge.main([str(mid), "helpful"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "no_session"


def test_session_id_from_env(temp_db, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-env")
    mid = _mem(temp_db)
    _surface(temp_db, "sess-env", mid)
    rc = judge.main([str(mid), "helpful"])
    assert rc == 0
    assert _counts(temp_db, mid) == (1, 0)
