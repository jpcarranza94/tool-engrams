"""watcher/runs_store.py — the watcher_runs + watcher_run_events seam."""

from __future__ import annotations

from toolengrams.watcher import runs_store


def _start(conn, sess="s", role="formation", pid=111, ts=1000, cur_from=0):
    return runs_store.start_run(
        conn, work_session_id=sess, role=role, pid=pid, started_ts=ts,
        model="opus", flush=False, cursor_from=cur_from, cwd="/repo",
    )


def test_start_run_is_running(temp_db):
    rid = _start(temp_db)
    row = temp_db.execute("SELECT * FROM watcher_runs WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "running"
    assert row["pid"] == 111
    assert row["ended_ts"] is None
    assert row["model"] == "opus"


def test_finish_run_finalizes(temp_db):
    rid = _start(temp_db)
    runs_store.finish_run(temp_db, rid, status="ok", ended_ts=1050,
                          cursor_to=42, delta_chars=900)
    row = temp_db.execute("SELECT * FROM watcher_runs WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "ok"
    assert row["ended_ts"] == 1050
    assert row["cursor_to"] == 42
    assert row["delta_chars"] == 900


def test_reap_stale_only_targets_session_role(temp_db):
    a1 = _start(temp_db, sess="A", role="formation", ts=1000)
    a2 = _start(temp_db, sess="A", role="formation", ts=1001)  # older stuck run
    b = _start(temp_db, sess="B", role="formation", ts=1002)
    ae = _start(temp_db, sess="A", role="eval", ts=1003)

    reaped = runs_store.reap_stale(temp_db, "A", "formation", now_ts=2000)
    assert reaped == 2

    def status(rid):
        return temp_db.execute("SELECT status FROM watcher_runs WHERE id = ?", (rid,)).fetchone()[0]
    assert status(a1) == status(a2) == "crashed"
    assert status(b) == "running"       # different session
    assert status(ae) == "running"      # different role


def test_record_event_and_recent_events(temp_db):
    rid = _start(temp_db, sess="A", role="eval")
    runs_store.record_event(temp_db, run_id=rid, ts=1100, kind="judged",
                            memory_id=42, memory_name="git-force", outcome="helpful")
    runs_store.record_event(temp_db, run_id=rid, ts=1101, kind="judged",
                            memory_id=43, memory_name="docker", outcome="noise")
    events = runs_store.recent_events(temp_db, limit=10)
    assert [e["memory_id"] for e in events] == [43, 42]  # newest first
    assert events[0]["role"] == "eval"
    assert events[0]["outcome"] == "noise"


def test_recent_runs_carries_event_counts(temp_db):
    rid = _start(temp_db, sess="A", role="formation", ts=5000)
    runs_store.finish_run(temp_db, rid, status="ok", ended_ts=5010)
    runs_store.record_event(temp_db, run_id=rid, ts=5005, kind="created",
                            memory_id=1, memory_name="m1")
    runs_store.record_event(temp_db, run_id=rid, ts=5006, kind="created",
                            memory_id=2, memory_name="m2")
    runs_store.record_event(temp_db, run_id=rid, ts=5007, kind="judged",
                            memory_id=3, memory_name="m3", outcome="unused")

    runs = runs_store.recent_runs(temp_db, since_ts=4000, limit=10)
    assert len(runs) == 1
    assert runs[0]["n_created"] == 2
    assert runs[0]["n_judged"] == 1


def test_active_runs_only_running(temp_db):
    r_run = _start(temp_db, sess="A", ts=1000)
    r_done = _start(temp_db, sess="B", ts=1001)
    runs_store.finish_run(temp_db, r_done, status="ok", ended_ts=1010)
    active = runs_store.active_runs(temp_db)
    assert [r["id"] for r in active] == [r_run]


def test_prune_runs_before_drops_runs_and_events(temp_db):
    old = _start(temp_db, sess="A", ts=1000)
    runs_store.record_event(temp_db, run_id=old, ts=1001, kind="created",
                            memory_id=1, memory_name="old")
    new = _start(temp_db, sess="A", ts=9000)

    deleted = runs_store.prune_runs_before(temp_db, cutoff_ts=5000)
    assert deleted == 1
    assert temp_db.execute("SELECT COUNT(*) FROM watcher_runs").fetchone()[0] == 1
    assert temp_db.execute("SELECT COUNT(*) FROM watcher_run_events").fetchone()[0] == 0
    assert temp_db.execute("SELECT id FROM watcher_runs").fetchone()[0] == new


def test_counts_since(temp_db):
    a = _start(temp_db, sess="A", ts=8000)
    runs_store.finish_run(temp_db, a, status="ok", ended_ts=8010)
    runs_store.record_event(temp_db, run_id=a, ts=8005, kind="created",
                            memory_id=1, memory_name="m")
    c = runs_store.counts_since(temp_db, since_ts=7000)
    assert c["runs_by_status"]["ok"] == 1
    assert c["created"] == 1
    assert c["judged"] == 0
