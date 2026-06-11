"""engram quarantine — eval's reversible emergency brake (ADR-0007)."""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import quarantine
from toolengrams.retrieval.session_state import (
    HOOK_PRE_TOOL_USE,
    has_pending_surfaces,
    log_surfaces,
)
from toolengrams.watcher import runs_store


def _seed(conn, useful=4) -> int:
    mid = memory_store.insert_memory(
        conn, name="harmful demo", description="", body="bad advice",
        kind="hint", scope="global", project_slug=None, pinned=False,
        created_ts=int(time.time()),
    )
    conn.execute("UPDATE memories SET useful_count = ? WHERE id = ?", (useful, mid))
    return mid


def test_quarantine_soft_demotes_and_marks_noise(temp_db, capsys):
    mid = _seed(temp_db)
    log_surfaces(temp_db, "sess-q", [mid], "tu-1", HOOK_PRE_TOOL_USE, 1,
                 int(time.time()))
    assert has_pending_surfaces(temp_db, "sess-q")

    rc = quarantine.main([str(mid), "--reason", "followed it and broke the build",
                          "--session-id", "sess-q"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "quarantined"
    assert out["surfaces_marked_noise"] == 1

    mem = memory_store.get(temp_db, mid)
    assert mem.useful_count == 0          # soft-demote zeroes usefulness
    assert mem.archived_ts is None        # NOT archived — reversible
    assert not has_pending_surfaces(temp_db, "sess-q")  # judged as noise


def test_quarantine_records_audit_event_in_watcher_context(temp_db, capsys, monkeypatch):
    mid = _seed(temp_db)
    run_id = runs_store.start_run(
        temp_db, work_session_id="sess-q2", role="eval", pid=1,
        started_ts=int(time.time()), model="sonnet", flush=False,
        cursor_from=0, cwd="/tmp",
    )
    monkeypatch.setenv("ENGRAM_RUN_ID", str(run_id))

    assert quarantine.main([str(mid), "--reason", "dangerous instruction"]) == 0
    ev = temp_db.execute(
        "SELECT kind, memory_id, detail FROM watcher_run_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert ev["kind"] == "quarantined"
    assert ev["memory_id"] == mid
    assert ev["detail"] == "dangerous instruction"


def test_quarantine_is_restorable(temp_db, capsys):
    mid = _seed(temp_db)
    quarantine.main([str(mid), "--reason", "r"])
    capsys.readouterr()
    memory_store.restore(temp_db, mid)
    mem = memory_store.get(temp_db, mid)
    assert mem.archived_ts is None and mem.surface_count == 0


def test_quarantine_id_only_and_not_found(temp_db, capsys):
    assert quarantine.main(["999", "--reason", "r"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_found"
