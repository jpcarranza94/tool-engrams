"""Tests for the consolidation_recommendations seam (consolidation/runs.py)."""

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


def _rec(title, severity="info", status="open", detail=None, issue_url=None) -> dict:
    return {"title": title, "severity": severity, "status": status,
            "detail": detail, "issue_url": issue_url}


def test_insert_and_read_back(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(
        temp_db, "2026-06-01",
        [_rec("noisy path glob", severity="warn", detail="fires on reads")],
        now_ts=1000,
    )
    rows = runs.recommendations_across_runs(temp_db, run_limit=10)
    assert len(rows) == 1
    assert rows[0]["title"] == "noisy path glob"
    assert rows[0]["severity"] == "warn"
    assert rows[0]["status"] == "open"
    assert rows[0]["detail"] == "fires on reads"
    assert rows[0]["created_ts"] == 1000
    assert rows[0]["resolved_ts"] is None


def test_done_status_stamps_resolved_ts(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(
        temp_db, "2026-06-01", [_rec("handled item", status="done")], now_ts=2000)
    row = runs.recommendations_across_runs(temp_db, run_limit=10)[0]
    assert row["status"] == "done"
    assert row["resolved_ts"] == 2000


def test_reinsert_replaces_that_days_set(temp_db):
    """A --force re-run replaces a day's recommendations wholesale (no dupes)."""
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(
        temp_db, "2026-06-01", [_rec("first"), _rec("second")], now_ts=1)
    runs.insert_recommendations(
        temp_db, "2026-06-01", [_rec("replacement")], now_ts=2)
    titles = [r["title"] for r in runs.recommendations_across_runs(temp_db, 10)]
    assert titles == ["replacement"]


def test_reinsert_one_day_leaves_other_days(temp_db):
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("day1")], now_ts=1)
    runs.insert_recommendations(temp_db, "2026-06-02", [_rec("day2")], now_ts=2)
    runs.insert_recommendations(temp_db, "2026-06-02", [_rec("day2-redo")], now_ts=3)
    titles = {r["title"] for r in runs.recommendations_across_runs(temp_db, 10)}
    assert titles == {"day1", "day2-redo"}


def test_empty_list_clears_the_day(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("x")], now_ts=1)
    runs.insert_recommendations(temp_db, "2026-06-01", [], now_ts=2)
    assert runs.recommendations_across_runs(temp_db, 10) == []


def test_across_runs_ordered_newest_first(temp_db):
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("older")], now_ts=1)
    runs.insert_recommendations(temp_db, "2026-06-02", [_rec("newer")], now_ts=2)
    dates = [r["run_date"] for r in runs.recommendations_across_runs(temp_db, 10)]
    assert dates == ["2026-06-02", "2026-06-01"]


def test_across_runs_respects_run_window(temp_db):
    """run_limit bounds by recent run_dates — recs on older runs fall outside."""
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    _record(temp_db, "2026-06-03", started_ts=300)
    for d, ts in [("2026-06-01", 100), ("2026-06-02", 200), ("2026-06-03", 300)]:
        runs.insert_recommendations(temp_db, d, [_rec(f"r-{d}")], now_ts=ts)
    # Only the 2 most recent runs are in window.
    dates = {r["run_date"] for r in runs.recommendations_across_runs(temp_db, run_limit=2)}
    assert dates == {"2026-06-03", "2026-06-02"}


# ---------- open_recommendations (WS5.1 backlog) ----------


def _titles_open(conn, run_limit=10):
    return [r["title"] for r in runs.open_recommendations(conn, run_limit)]


def test_open_backlog_lists_open_non_critical(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(
        temp_db, "2026-06-01",
        [_rec("noisy glob", severity="warn"),
         _rec("info trend", severity="info")],
        now_ts=1000,
    )
    assert set(_titles_open(temp_db)) == {"noisy glob", "info trend"}


def test_open_backlog_excludes_done(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(
        temp_db, "2026-06-01",
        [_rec("resolved item", status="done"), _rec("still open")],
        now_ts=1000,
    )
    assert _titles_open(temp_db) == ["still open"]


def test_open_backlog_excludes_critical(temp_db):
    """Critical (code-bug / data-safety) recs are maintainer-owned — the agent
    can't verify a code fix shipped, so they never enter the injected backlog."""
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(
        temp_db, "2026-06-01",
        [_rec("code bug", severity="critical"), _rec("mem noise", severity="warn")],
        now_ts=1000,
    )
    assert _titles_open(temp_db) == ["mem noise"]


def test_open_backlog_newest_status_wins_per_title(temp_db):
    """A title re-emitted `done` on a later run drops out of the backlog even
    though an earlier run left it open (newest-status per casefolded title)."""
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("Recurring")], now_ts=100)
    runs.insert_recommendations(
        temp_db, "2026-06-02", [_rec("recurring", status="done")], now_ts=200)
    assert _titles_open(temp_db) == []


def test_open_backlog_respects_run_window(temp_db):
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    _record(temp_db, "2026-06-03", started_ts=300)
    for d, ts in [("2026-06-01", 100), ("2026-06-02", 200), ("2026-06-03", 300)]:
        runs.insert_recommendations(temp_db, d, [_rec(f"r-{d}")], now_ts=ts)
    titles = set(_titles_open(temp_db, run_limit=2))
    assert titles == {"r-2026-06-03", "r-2026-06-02"}


# ---------- resolve_recommendation (WS5.2 manual close) ----------


def test_resolve_marks_all_rows_done(temp_db):
    _record(temp_db, "2026-06-01", started_ts=100)
    _record(temp_db, "2026-06-02", started_ts=200)
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("dupe issue")], now_ts=100)
    runs.insert_recommendations(temp_db, "2026-06-02", [_rec("dupe issue")], now_ts=200)
    updated = runs.resolve_recommendation(temp_db, "dupe issue", now_ts=9000)
    assert updated == 2
    rows = runs.recommendations_across_runs(temp_db, 10)
    assert all(r["status"] == "done" and r["resolved_ts"] == 9000 for r in rows)


def test_resolve_is_casefolded(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("Path Glob Noise")], now_ts=1)
    assert runs.resolve_recommendation(temp_db, "path glob noise", now_ts=2) == 1


def test_resolve_unknown_title_returns_zero(temp_db):
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("x")], now_ts=1)
    assert runs.resolve_recommendation(temp_db, "nonexistent", now_ts=2) == 0


def test_manual_close_is_sticky_against_backlog(temp_db):
    """A manual close drops the title from the OPEN backlog, so the agent is
    never re-prompted to raise it — the stickiness mechanism (WS5.2)."""
    _record(temp_db, "2026-06-01")
    runs.insert_recommendations(temp_db, "2026-06-01", [_rec("close me", severity="warn")], now_ts=1)
    assert "close me" in _titles_open(temp_db)
    runs.resolve_recommendation(temp_db, "close me", now_ts=2)
    assert "close me" not in _titles_open(temp_db)
