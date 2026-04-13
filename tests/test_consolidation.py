"""Unit tests for mechanical consolidation (Phase 2)."""

from __future__ import annotations

import json
import time

from toolengrams.consolidation.adjust import (
    ARCHIVE_MIN_SURFACES,
    ARCHIVE_USEFULNESS_THRESHOLD,
    AdjustmentReport,
    run_mechanical_adjustments,
)


def _seed(conn, name: str, surface_count: int = 0, useful_count: int = 0,
          type_: str = "reference", last_surfaced_ts: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, body, type, scope, created_ts, surface_count, useful_count, last_surfaced_ts) "
        "VALUES (?, 'body', ?, 'global', ?, ?, ?, ?)",
        (name, type_, int(time.time()), surface_count, useful_count, last_surfaced_ts),
    )
    return cur.lastrowid


# ---------- auto-archive ----------


def test_archive_dead_memory(temp_db):
    """Memory surfaced 10 times but useful 0 → usefulness 1/12 ≈ 0.08 → archived."""
    mid = _seed(temp_db, "dead", surface_count=10, useful_count=0)
    report = run_mechanical_adjustments(temp_db)
    assert mid in report.archived_ids
    row = temp_db.execute("SELECT archived_ts FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["archived_ts"] is not None


def test_healthy_memory_not_archived(temp_db):
    """Memory surfaced 10 times, useful 8 → usefulness 9/12 ≈ 0.75 → kept."""
    mid = _seed(temp_db, "healthy", surface_count=10, useful_count=8)
    report = run_mechanical_adjustments(temp_db)
    assert mid not in report.archived_ids


def test_low_surface_count_not_archived(temp_db):
    """Memory surfaced only 3 times with 0 useful → below threshold, kept."""
    mid = _seed(temp_db, "new", surface_count=3, useful_count=0)
    report = run_mechanical_adjustments(temp_db)
    assert mid not in report.archived_ids


# ---------- stale detection ----------


def test_stale_memory_flagged(temp_db):
    """Reference memory not surfaced in 120+ days (2× 60d half-life) → stale."""
    old_ts = int(time.time()) - (130 * 86400)
    mid = _seed(temp_db, "stale", last_surfaced_ts=old_ts, type_="reference")
    report = run_mechanical_adjustments(temp_db)
    assert mid in report.stale_ids


def test_recent_memory_not_stale(temp_db):
    """Memory surfaced yesterday → not stale."""
    recent_ts = int(time.time()) - 86400
    mid = _seed(temp_db, "recent", last_surfaced_ts=recent_ts)
    report = run_mechanical_adjustments(temp_db)
    assert mid not in report.stale_ids


def test_never_surfaced_not_stale(temp_db):
    """Memory never surfaced (last_surfaced_ts=0) → not flagged (hasn't had a chance)."""
    mid = _seed(temp_db, "fresh", last_surfaced_ts=0)
    report = run_mechanical_adjustments(temp_db)
    assert mid not in report.stale_ids


# ---------- session surfaces cleanup ----------


def test_old_surfaces_cleaned(temp_db):
    mid = _seed(temp_db, "mem")
    old_ts = int(time.time()) - (60 * 86400)  # 60 days ago
    temp_db.execute(
        "INSERT INTO session_surfaces (session_id, memory_id, surfaced_ts, hook) "
        "VALUES ('old-sess', ?, ?, 'pre_tool_use')",
        (mid, old_ts),
    )
    report = run_mechanical_adjustments(temp_db)
    assert report.surfaces_cleaned == 1
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM session_surfaces").fetchone()
    assert rows["c"] == 0


def test_recent_surfaces_kept(temp_db):
    mid = _seed(temp_db, "mem")
    recent_ts = int(time.time()) - 86400  # yesterday
    temp_db.execute(
        "INSERT INTO session_surfaces (session_id, memory_id, surfaced_ts, hook) "
        "VALUES ('recent-sess', ?, ?, 'pre_tool_use')",
        (mid, recent_ts),
    )
    report = run_mechanical_adjustments(temp_db)
    assert report.surfaces_cleaned == 0


# ---------- CLI ----------


def test_consolidate_dry_run(temp_db, monkeypatch, capsys):
    from toolengrams.commands import consolidate
    monkeypatch.setenv("ENGRAM_DB", str(temp_db.execute("PRAGMA database_list").fetchone()[2]))
    # Just verify it doesn't crash — real sessions won't exist in temp DB
    rc = consolidate.main(["--dry-run", "--json"])
    assert rc == 0


def test_consolidate_idempotent(temp_db, monkeypatch, capsys):
    from toolengrams.commands import consolidate
    # First run
    consolidate.main(["--json"])
    capsys.readouterr()
    # Second run should be idempotent
    consolidate.main(["--json"])
    out = capsys.readouterr().out
    result = json.loads(out.strip().splitlines()[-1])
    assert result["status"] == "already_run"
