"""Nightly consolidation prunes watcher_runs older than the TTL (monitor history
housekeeping), alongside the existing session_surfaces prune."""

from __future__ import annotations

import json
import time

from toolengrams.cli import consolidate
from toolengrams.watcher import runs_store


def _run(conn, started_ts) -> int:
    return runs_store.start_run(
        conn, work_session_id="s", role="formation", pid=1, started_ts=started_ts,
        model="opus", flush=False, cursor_from=0, cwd="/c",
    )


def test_consolidate_prunes_old_watcher_runs(temp_db, monkeypatch, capsys):
    now = int(time.time())
    old = _run(temp_db, now - 30 * 86400)   # older than the 14d TTL
    fresh = _run(temp_db, now)
    runs_store.record_event(temp_db, run_id=old, ts=now - 30 * 86400,
                            kind="created", memory_id=1, memory_name="m")

    # Housekeeping only runs when there are sessions; the elements aren't read
    # before the dry-run return, so a non-empty stub suffices.
    monkeypatch.setattr(consolidate, "collect_sessions", lambda target: ["x"])

    rc = consolidate.main(["--dry-run", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["watcher_runs_would_clean"] == 1

    ids = {r["id"] for r in temp_db.execute("SELECT id FROM watcher_runs").fetchall()}
    assert old not in ids and fresh in ids
    # The old run's events were pruned too.
    assert temp_db.execute("SELECT COUNT(*) FROM watcher_run_events").fetchone()[0] == 0
