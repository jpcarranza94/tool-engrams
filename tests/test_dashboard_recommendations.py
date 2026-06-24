"""Cross-run recommendation grouping/dedup for the dashboard (issue #64)."""

from __future__ import annotations

from toolengrams.cli.dashboard import _group_recommendations, _build_html
from toolengrams.consolidation import runs


def _row(run_date, title, severity="info", status="open", detail=None, issue_url=None):
    return {"run_date": run_date, "title": title, "severity": severity,
            "status": status, "detail": detail, "issue_url": issue_url}


def test_empty_input():
    assert _group_recommendations([]) == []


def test_recurring_title_collapses_with_all_dates():
    rows = [  # newest-first, as the query returns
        _row("2026-06-23", "path-glob read-vs-write noise"),
        _row("2026-06-17", "path-glob read-vs-write noise"),
        _row("2026-06-16", "path-glob read-vs-write noise"),
    ]
    grouped = _group_recommendations(rows)
    assert len(grouped) == 1
    assert grouped[0]["dates"] == ["2026-06-23", "2026-06-17", "2026-06-16"]


def test_dedup_is_casefold_and_strip_insensitive():
    rows = [_row("2026-06-23", "Noisy Trigger"),
            _row("2026-06-22", "  noisy trigger  ")]
    grouped = _group_recommendations(rows)
    assert len(grouped) == 1
    # Display title comes from the most recent occurrence.
    assert grouped[0]["title"] == "Noisy Trigger"
    assert grouped[0]["dates"] == ["2026-06-23", "2026-06-22"]


def test_status_from_most_recent_occurrence():
    rows = [_row("2026-06-23", "x", status="done"),
            _row("2026-06-20", "x", status="open")]
    assert _group_recommendations(rows)[0]["status"] == "done"


def test_severity_is_max_across_occurrences():
    rows = [_row("2026-06-23", "x", severity="info"),
            _row("2026-06-20", "x", severity="critical"),
            _row("2026-06-19", "x", severity="warn")]
    assert _group_recommendations(rows)[0]["severity"] == "critical"


def test_detail_and_issue_url_backfill_from_older_when_latest_empty():
    rows = [_row("2026-06-23", "x", detail=None, issue_url=None),
            _row("2026-06-20", "x", detail="older detail",
                 issue_url="https://example.com/9")]
    g = _group_recommendations(rows)[0]
    assert g["detail"] == "older detail"
    assert g["issue_url"] == "https://example.com/9"


def test_sorted_severity_then_newest_date_first():
    rows = [
        _row("2026-06-10", "info-old", severity="info"),
        _row("2026-06-23", "warn-new", severity="warn"),
        _row("2026-06-22", "crit", severity="critical"),
        _row("2026-06-21", "warn-older", severity="warn"),
    ]
    titles = [g["title"] for g in _group_recommendations(rows)]
    # critical first; then the two warns newest-date-first; then info.
    assert titles == ["crit", "warn-new", "warn-older", "info-old"]


def test_build_html_renders_recommendations_tab(temp_db):
    """End-to-end: a persisted recommendation appears in the rendered dashboard,
    deduped, with its issue link and both dates."""
    runs.record_run(
        temp_db, run_date="2026-06-23", started_ts=200, completed_ts=2,
        sessions_scanned=1, episodes_evaluated=0, memories_weakened=0,
        memories_archived=0, memories_discovered=0, report="r", quality_score=0.5,
        surfaces_helpful=0, surfaces_noise=0, memories_verified=0)
    runs.record_run(
        temp_db, run_date="2026-06-16", started_ts=100, completed_ts=2,
        sessions_scanned=1, episodes_evaluated=0, memories_weakened=0,
        memories_archived=0, memories_discovered=0, report="r", quality_score=0.5,
        surfaces_helpful=0, surfaces_noise=0, memories_verified=0)
    runs.insert_recommendations(
        temp_db, "2026-06-23",
        [{"title": "recurring noise", "severity": "warn", "status": "open",
          "detail": "still happening", "issue_url": "https://example.com/64"}],
        now_ts=200)
    runs.insert_recommendations(
        temp_db, "2026-06-16",
        [{"title": "recurring noise", "severity": "info", "status": "open",
          "detail": None, "issue_url": None}],
        now_ts=100)

    out = _build_html(temp_db)
    assert 'data-tab="recommendations"' in out
    assert "recurring noise" in out
    assert "https://example.com/64" in out
    assert "2026-06-23" in out and "2026-06-16" in out
    # Deduped: the title text appears once in the recommendations rows.
    assert out.count(">recurring noise<") == 1
    # Max severity wins (warn), not the older info.
    assert "tag sev-warn" in out
