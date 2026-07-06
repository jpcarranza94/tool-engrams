"""`engram recommend --list` / `--close` CLI (WS5.2)."""

from __future__ import annotations

import json

from toolengrams.cli import recommend
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


def _rec(title, severity="info", status="open", detail=None) -> dict:
    return {"title": title, "severity": severity, "status": status,
            "detail": detail, "issue_url": None}


def _seed(conn):
    _record(conn, "2026-06-01")
    runs.insert_recommendations(
        conn, "2026-06-01",
        [_rec("noisy glob", severity="warn", detail="fires on reads"),
         _rec("shipped fix", severity="critical"),
         _rec("already done", status="done")],
        now_ts=1000,
    )


def test_list_shows_open_non_critical(temp_db, capsys):
    _seed(temp_db)
    rc = recommend.main(["--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "list"
    titles = [r["title"] for r in out["open"]]
    # critical + done excluded; only the open, non-critical warn remains.
    assert titles == ["noisy glob"]
    assert out["open"][0]["detail"] == "fires on reads"
    assert out["count"] == 1


def test_list_is_default_without_flags(temp_db, capsys):
    _seed(temp_db)
    rc = recommend.main([])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "list"


def test_close_marks_done_and_reports(temp_db, capsys):
    _seed(temp_db)
    rc = recommend.main(["--close", "noisy glob"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "closed"
    assert out["rows_updated"] == 1
    # And it's gone from the backlog now.
    assert runs.open_recommendations(temp_db, 10) == []


def test_close_casefolded(temp_db, capsys):
    _seed(temp_db)
    rc = recommend.main(["--close", "NOISY GLOB"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["rows_updated"] == 1


def test_close_unknown_returns_1(temp_db, capsys):
    _seed(temp_db)
    rc = recommend.main(["--close", "does not exist"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["action"] == "not_found"


def test_close_strips_padded_title(temp_db, capsys):
    _seed(temp_db)
    rc = recommend.main(["--close", "  noisy glob  "])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["rows_updated"] == 1


def test_close_forbidden_under_agent_context(temp_db, capsys, monkeypatch):
    # The nightly consolidation agent must not close a rec (only a maintainer
    # can); the guard is code-enforced via the CONSOLIDATION_CHILD_ENV marker.
    _seed(temp_db)
    monkeypatch.setenv("ENGRAM_IN_CONSOLIDATION", "1")
    rc = recommend.main(["--close", "noisy glob"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "forbidden"
    assert out["reason"] == "agent_context"
    # Untouched — still open in the backlog.
    assert [r["title"] for r in runs.open_recommendations(temp_db, 10)] == ["noisy glob"]


def test_list_spans_all_runs_beyond_agent_window(temp_db, capsys):
    # An open rec raised in the oldest run, then MORE runs than the agent's
    # bounded window (OPEN_BACKLOG_RUN_WINDOW=7) with no new recs.
    _record(temp_db, "2026-06-01", started_ts=100)
    runs.insert_recommendations(
        temp_db, "2026-06-01", [_rec("aged open", severity="warn")], now_ts=100)
    for i in range(2, 12):  # 10 newer, rec-less runs
        _record(temp_db, f"2026-06-{i:02d}", started_ts=100 + i)
    # The agent's bounded window ages it out...
    assert runs.open_recommendations(temp_db, runs.OPEN_BACKLOG_RUN_WINDOW) == []
    # ...but the maintainer --list (ALL_RUNS) still surfaces it.
    rc = recommend.main(["--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["title"] for r in out["open"]] == ["aged open"]
