"""Tests for the consolidation_runs persistence seam (consolidation/runs.py)."""

from __future__ import annotations

from toolengrams.consolidation import runs


def _record(conn, run_date, **over) -> None:
    fields = dict(
        run_date=run_date, started_ts=1, completed_ts=2, sessions_scanned=3,
        episodes_evaluated=4, memories_weakened=5, memories_archived=6,
        memories_discovered=7, report="r", quality_score=0.5,
        surfaces_helpful=8, surfaces_noise=9, memories_verified=10,
    )
    fields.update(over)
    runs.record_run(conn, **fields)


def test_was_run(temp_db):
    assert runs.was_run(temp_db, "2026-06-01") is False
    _record(temp_db, "2026-06-01")
    assert runs.was_run(temp_db, "2026-06-01") is True


def test_record_run_upserts_by_date(temp_db):
    _record(temp_db, "2026-06-02", sessions_scanned=3)
    _record(temp_db, "2026-06-02", sessions_scanned=99)  # run_date UNIQUE → replace
    assert runs.last_run(temp_db)["sessions_scanned"] == 99


def test_last_and_recent_runs_order_newest_first(temp_db):
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    assert runs.last_run(temp_db)["run_date"] == "2026-06-02"
    assert [r["run_date"] for r in runs.recent_runs(temp_db, limit=10)] == \
        ["2026-06-02", "2026-06-01"]


def test_last_run_none_when_empty(temp_db):
    assert runs.last_run(temp_db) is None


def test_record_run_persists_every_column(temp_db):
    """Lock the 14-column INSERT mapping (the blast radius): a distinct value per
    column must land in its own column, with memories_strengthened defaulting 0."""
    runs.record_run(
        temp_db,
        run_date="2026-06-09", started_ts=11, completed_ts=22, sessions_scanned=33,
        episodes_evaluated=44, memories_weakened=55, memories_archived=66,
        memories_discovered=77, report="the report", quality_score=0.875,
        surfaces_helpful=88, surfaces_noise=99, memories_verified=111,
    )
    row = temp_db.execute(
        "SELECT run_date, started_ts, completed_ts, sessions_scanned, episodes_evaluated, "
        "memories_strengthened, memories_weakened, memories_archived, memories_discovered, "
        "report, quality_score, surfaces_helpful, surfaces_noise, memories_verified "
        "FROM consolidation_runs WHERE run_date = '2026-06-09'"
    ).fetchone()
    assert dict(row) == {
        "run_date": "2026-06-09", "started_ts": 11, "completed_ts": 22,
        "sessions_scanned": 33, "episodes_evaluated": 44,
        "memories_strengthened": 0,  # hardcoded default
        "memories_weakened": 55, "memories_archived": 66, "memories_discovered": 77,
        "report": "the report", "quality_score": 0.875,
        "surfaces_helpful": 88, "surfaces_noise": 99, "memories_verified": 111,
    }
