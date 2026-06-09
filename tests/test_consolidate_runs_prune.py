"""Nightly consolidation prunes watcher_runs older than the TTL (monitor history
housekeeping) on a real run — and a --dry-run only PREVIEWS, never deletes."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from toolengrams.cli import consolidate
from toolengrams.watcher import runs_store


def _run(conn, started_ts) -> int:
    return runs_store.start_run(
        conn, work_session_id="s", role="formation", pid=1, started_ts=started_ts,
        model="opus", flush=False, cursor_from=0, cwd="/c",
    )


def _ids(conn):
    return {r["id"] for r in conn.execute("SELECT id FROM watcher_runs").fetchall()}


def test_dry_run_previews_but_does_not_prune(temp_db, monkeypatch, capsys):
    now = int(time.time())
    old = _run(temp_db, now - 30 * 86400)   # older than the 14d TTL
    fresh = _run(temp_db, now)
    monkeypatch.setattr(consolidate, "collect_sessions", lambda target: ["x"])

    rc = consolidate.main(["--dry-run", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["watcher_runs_would_clean"] == 1
    # Nothing was actually deleted — dry-run is a no-op preview.
    assert _ids(temp_db) == {old, fresh}


def test_real_run_prunes_old_watcher_runs(temp_db, monkeypatch, capsys):
    now = int(time.time())
    old = _run(temp_db, now - 30 * 86400)
    fresh = _run(temp_db, now)
    runs_store.record_event(temp_db, run_id=old, ts=now - 30 * 86400,
                            kind="created", memory_id=1, memory_name="m")
    monkeypatch.setattr(consolidate, "collect_sessions", lambda target: ["x"])
    monkeypatch.setattr(consolidate, "run_consolidation_agent",
                        lambda **kw: SimpleNamespace(error=None, report="{}", returncode=0))

    rc = consolidate.main(["--json"])
    assert rc == 0
    assert _ids(temp_db) == {fresh}         # old run pruned
    assert temp_db.execute("SELECT COUNT(*) FROM watcher_run_events").fetchone()[0] == 0
