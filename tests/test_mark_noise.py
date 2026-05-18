"""`engram mark-noise <name>` — retroactively mark unmarked surfaces as noise."""

from __future__ import annotations

import json
import time

from toolengrams.cli import mark_noise


def _seed_memory(conn, name: str, archived: bool = False) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts, archived_ts) "
        "VALUES (?, '', 'b', 'hint', 'global', NULL, ?, ?)",
        (name, now_ts, now_ts if archived else None),
    )
    return cur.lastrowid


def _seed_surface(
    conn, session_id: str, memory_id: int, surfaced_ts: int,
    hook: str = "pre_tool_use", outcome: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface, outcome) "
        "VALUES (?, ?, ?, ?, 'tu-x', 1, ?)",
        (session_id, memory_id, surfaced_ts, hook, outcome),
    )


def test_mark_noise_all_sessions(temp_db, capsys):
    mid = _seed_memory(temp_db, "noisy")
    _seed_surface(temp_db, "sess-a", mid, 1000)
    _seed_surface(temp_db, "sess-b", mid, 2000)
    _seed_surface(temp_db, "sess-c", mid, 3000)

    rc = mark_noise.main(["noisy"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "marked_noise"
    assert payload["rows_updated"] == 3

    rows = temp_db.execute(
        "SELECT outcome FROM session_surfaces WHERE memory_id = ?",
        (mid,),
    ).fetchall()
    assert all(r["outcome"] == "noise" for r in rows)


def test_mark_noise_scoped_to_session(temp_db, capsys):
    mid = _seed_memory(temp_db, "scoped")
    _seed_surface(temp_db, "sess-a", mid, 1000)
    _seed_surface(temp_db, "sess-b", mid, 2000)

    rc = mark_noise.main(["scoped", "--session-id", "sess-a"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows_updated"] == 1

    rows = dict(
        temp_db.execute(
            "SELECT session_id, outcome FROM session_surfaces WHERE memory_id = ?",
            (mid,),
        ).fetchall()
    )
    # The non-scoped session's surface stays NULL.
    assert rows["sess-a"] == "noise"
    assert rows["sess-b"] is None


def test_mark_noise_does_not_overwrite_helpful(temp_db, capsys):
    mid = _seed_memory(temp_db, "mixed")
    _seed_surface(temp_db, "sess-a", mid, 1000, outcome="helpful")
    _seed_surface(temp_db, "sess-a", mid, 2000)  # unmarked

    rc = mark_noise.main(["mixed"])
    assert rc == 0

    rows = temp_db.execute(
        "SELECT surfaced_ts, outcome FROM session_surfaces "
        "WHERE memory_id = ? ORDER BY surfaced_ts",
        (mid,),
    ).fetchall()
    # helpful stays, unmarked becomes noise.
    assert rows[0]["outcome"] == "helpful"
    assert rows[1]["outcome"] == "noise"


def test_mark_noise_noop_when_no_unmarked(temp_db, capsys):
    mid = _seed_memory(temp_db, "all-marked")
    _seed_surface(temp_db, "sess-a", mid, 1000, outcome="helpful")

    rc = mark_noise.main(["all-marked"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop"
    assert payload["reason"] == "no_unmarked_surfaces"


def test_mark_noise_unknown_memory(temp_db, capsys):
    rc = mark_noise.main(["does-not-exist"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


def test_mark_noise_works_on_archived_memory(temp_db, capsys):
    """Archived memories can still have their past surfaces marked noise —
    the consolidation agent may want to label noisy surfaces of a memory
    it just archived in the same run."""
    mid = _seed_memory(temp_db, "dead-but-tagged", archived=True)
    _seed_surface(temp_db, "sess-a", mid, 1000)

    rc = mark_noise.main(["dead-but-tagged"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows_updated"] == 1
