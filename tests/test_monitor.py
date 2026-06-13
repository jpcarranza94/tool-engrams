"""engram monitor — the headless data layer (build_snapshot, liveness, JSON path).

The live rich rendering is display code and isn't unit-tested; everything it
draws comes from these pure functions.
"""

from __future__ import annotations

import json
import os
import time

from toolengrams.cli import monitor
from toolengrams.watcher import runs_store


def _seed(conn, now):
    rid = runs_store.start_run(
        conn, work_session_id="sess-abcdef12", role="formation", pid=os.getpid(),
        started_ts=now - 5, model="opus", flush=False, cursor_from=0, cwd="/repo",
        engine="claude-code",
    )
    runs_store.finish_run(conn, rid, status="ok", ended_ts=now - 2,
                          cursor_to=10, delta_chars=400)
    runs_store.record_event(conn, run_id=rid, ts=now - 3, kind="created",
                            memory_id=1, memory_name="gfp")
    erid = runs_store.start_run(
        conn, work_session_id="sess-abcdef12", role="eval", pid=os.getpid(),
        started_ts=now - 1, model="opus", flush=False, cursor_from=0, cwd="/repo",
        engine="codex",
    )
    runs_store.record_event(conn, run_id=erid, ts=now, kind="judged",
                            memory_id=2, memory_name="dock", outcome="noise")
    return rid, erid


def test_build_snapshot_shape(temp_db):
    now = int(time.time())
    _seed(temp_db, now)
    snap = monitor.build_snapshot(temp_db, now)

    assert set(snap) == {"now", "active", "recent_24h", "stream", "counts_24h"}
    # The eval run is still 'running' → it shows in active.
    assert any(a["role"] == "eval" for a in snap["active"])
    # History has both runs; counts reflect them.
    assert len(snap["recent_24h"]) == 2
    assert {r["engine"] for r in snap["recent_24h"]} == {"claude-code", "codex"}
    assert snap["counts_24h"]["created"] == 1
    assert snap["counts_24h"]["judged"] == 1
    # Stream newest first: the judged event leads.
    assert snap["stream"][0]["kind"] == "judged"
    assert snap["stream"][0]["outcome"] == "noise"


def test_active_view_active_when_pid_alive_and_fresh():
    now = int(time.time())
    row = {"work_session_id": "s", "role": "formation", "started_ts": now - 3,
           "pid": os.getpid(), "cwd": "/r", "engine": "claude-code"}
    assert monitor._active_view(row, now)["state"] == "active"


def test_active_view_stale_when_pid_dead():
    now = int(time.time())
    row = {"work_session_id": "s", "role": "formation", "started_ts": now - 3,
           "pid": 2_147_483_000, "cwd": "/r",  # almost certainly not live
           "engine": "claude-code"}
    assert monitor._active_view(row, now)["state"] == "stale"


def test_active_view_stale_when_old_even_if_pid_alive():
    now = int(time.time())
    row = {"work_session_id": "s", "role": "formation",
           "started_ts": now - (monitor._stale_after_sec() + 10),
           "pid": os.getpid(), "cwd": "/r", "engine": "claude-code"}
    assert monitor._active_view(row, now)["state"] == "stale"


def test_build_snapshot_empty_db(temp_db):
    snap = monitor.build_snapshot(temp_db, int(time.time()))
    assert snap["active"] == [] and snap["recent_24h"] == [] and snap["stream"] == []
    assert snap["counts_24h"]["created"] == 0 and snap["counts_24h"]["judged"] == 0


def test_pid_alive_self_and_dead():
    assert monitor._pid_alive(os.getpid()) is True
    assert monitor._pid_alive(2_147_483_000) is False
    assert monitor._pid_alive(None) is False


def test_main_json_snapshot(temp_db, capsys):
    now = int(time.time())
    _seed(temp_db, now)
    rc = monitor.main(["--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"now", "active", "recent_24h", "stream", "counts_24h"}
    assert len(out["recent_24h"]) == 2
    assert "engine" in out["recent_24h"][0]
