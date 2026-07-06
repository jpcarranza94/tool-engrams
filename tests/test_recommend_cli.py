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
