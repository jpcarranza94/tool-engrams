"""engram remember / judge record a watcher_run_events row when running inside a
watcher session ($ENGRAM_RUN_ID set), and nothing otherwise."""

from __future__ import annotations

import time

from toolengrams import memory_store
from toolengrams.cli import judge, remember
from toolengrams.retrieval import session_state
from toolengrams.watcher import runs_store

_REMEMBER = ["Use --force-with-lease, never --force.", "--trigger", "git push --force",
             "--name", "gfp", "--scope", "global", "--kind", "block"]


def _run(conn, role) -> int:
    return runs_store.start_run(
        conn, work_session_id="s", role=role, pid=1, started_ts=int(time.time()),
        model="opus", flush=False, cursor_from=0, cwd="/c",
    )


def _events(conn):
    return conn.execute(
        "SELECT kind, memory_id, memory_name, outcome, run_id FROM watcher_run_events"
    ).fetchall()


def test_remember_records_created_event(temp_db, monkeypatch, capsys):
    rid = _run(temp_db, "formation")
    monkeypatch.setenv("ENGRAM_RUN_ID", str(rid))
    assert remember.main(_REMEMBER) == 0
    ev = _events(temp_db)
    assert len(ev) == 1
    assert ev[0]["kind"] == "created"
    assert ev[0]["memory_name"] == "gfp"
    assert ev[0]["run_id"] == rid


def test_remember_no_event_without_run_id(temp_db, monkeypatch, capsys):
    monkeypatch.delenv("ENGRAM_RUN_ID", raising=False)
    assert remember.main(_REMEMBER) == 0
    assert _events(temp_db) == []


def test_judge_records_judged_event_with_outcome(temp_db, monkeypatch, capsys):
    mid = memory_store.insert_memory(
        temp_db, name="m", description="", body="b", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    session_state.log_surfaces(temp_db, "s", [mid], "tu", "pre_tool_use", 1, int(time.time()))
    rid = _run(temp_db, "eval")
    monkeypatch.setenv("ENGRAM_RUN_ID", str(rid))

    assert judge.main([str(mid), "noise", "--session-id", "s"]) == 0
    ev = _events(temp_db)
    assert len(ev) == 1
    assert ev[0]["kind"] == "judged"
    assert ev[0]["memory_id"] == mid
    assert ev[0]["outcome"] == "noise"
    assert ev[0]["run_id"] == rid


def test_judge_reject_records_no_event(temp_db, monkeypatch, capsys):
    # Memory exists but never surfaced in this session → not_in_session, no event.
    mid = memory_store.insert_memory(
        temp_db, name="m", description="", body="b", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    rid = _run(temp_db, "eval")
    monkeypatch.setenv("ENGRAM_RUN_ID", str(rid))

    assert judge.main([str(mid), "helpful", "--session-id", "s"]) == 1
    assert _events(temp_db) == []
