"""Unit tests for consolidation CLI and session collection."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from toolengrams.cli import consolidate
from toolengrams.consolidation import agent
from toolengrams.engine import EngineResult
from toolengrams.target.claude_code.collect import collect_sessions
from toolengrams.target.interface import SessionFile


# ---------- session collection ----------


def test_collect_finds_jsonl_from_target_date(tmp_path):
    project_dir = tmp_path / "projects" / "my-project"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "abc-123.jsonl"
    jsonl.write_text('{"type":"user"}\n')

    sessions = collect_sessions(date.today(), projects_dir=tmp_path / "projects")
    assert len(sessions) == 1
    assert sessions[0].session_id == "abc-123"
    assert sessions[0].project_slug == "my-project"


def test_collect_ignores_other_dates(tmp_path):
    project_dir = tmp_path / "projects" / "my-project"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "old-session.jsonl"
    jsonl.write_text('{"type":"user"}\n')
    # Backdate the file.
    old_ts = time.time() - 86400 * 5
    os.utime(jsonl, (old_ts, old_ts))

    sessions = collect_sessions(date.today(), projects_dir=tmp_path / "projects")
    assert len(sessions) == 0


def test_consolidation_collects_wired_targets_in_timestamp_order(monkeypatch):
    target = date.today()
    claude_session = SessionFile(
        path=Path("/sessions/claude.jsonl"),
        session_id="claude-session",
        project_slug="proj",
        modified_ts=20,
        size_bytes=200,
    )
    codex_session = SessionFile(
        path=Path("/sessions/codex.jsonl"),
        session_id="codex-session",
        project_slug="proj",
        modified_ts=10,
        size_bytes=100,
    )
    skipped = SimpleNamespace(
        NAME="off",
        is_wired=lambda: False,
        collect_sessions=lambda target_date: (_ for _ in ()).throw(
            AssertionError("unwired target called")
        ),
    )
    targets = {
        "claude-code": SimpleNamespace(
            NAME="claude-code",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: [claude_session],
        ),
        "codex": SimpleNamespace(
            NAME="codex",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: [codex_session],
        ),
        "off": skipped,
    }
    monkeypatch.setattr(consolidate, "TARGETS", targets)

    sessions = consolidate.collect_sessions(target)

    assert [(s.target, s.session_id) for s in sessions] == [
        ("codex", "codex-session"),
        ("claude-code", "claude-session"),
    ]


def test_consolidation_collects_other_targets_when_one_target_fails(
    monkeypatch, capsys,
):
    target = date.today()
    good_session = SessionFile(
        path=Path("/sessions/claude.jsonl"),
        session_id="claude-session",
        project_slug="proj",
        modified_ts=20,
        size_bytes=200,
    )
    targets = {
        "claude-code": SimpleNamespace(
            NAME="claude-code",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: [good_session],
        ),
        "codex": SimpleNamespace(
            NAME="codex",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: (_ for _ in ()).throw(
                RuntimeError("bad rollout")
            ),
        ),
    }
    monkeypatch.setattr(consolidate, "TARGETS", targets)

    sessions = consolidate.collect_sessions(target)

    assert [(s.target, s.session_id) for s in sessions] == [
        ("claude-code", "claude-session"),
    ]
    assert "codex collection failed: bad rollout" in capsys.readouterr().err


def test_consolidation_prompt_session_list_includes_target(monkeypatch, tmp_path):
    captured = {}

    def invoke(req):
        captured["prompt"] = req.prompt
        return EngineResult(ok=True, returncode=0, text="done")

    fake_engine = SimpleNamespace(
        NAME="fake",
        is_available=lambda: True,
        prepare_sandbox=lambda path, spec: None,
        invoke=invoke,
    )
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setattr(agent, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(agent, "_get_memory_summary", lambda path: "memory summary")
    session = SessionFile(
        path=tmp_path / "rollout.jsonl",
        session_id="session-abcdef",
        project_slug="proj",
        modified_ts=1,
        size_bytes=1024,
        target="codex",
    )

    result = agent.run_consolidation_agent([session], db_path, "2026-06-12")

    assert result.returncode == 0
    assert "[codex]" in captured["prompt"]
    assert str(session.path) in captured["prompt"]


# ---------- CLI ----------


def test_consolidate_dry_run(temp_db, monkeypatch, capsys):
    rc = consolidate.main(["--dry-run", "--json"])
    assert rc == 0


def test_consolidate_idempotent(temp_db, monkeypatch, capsys):
    # Simulate a previous run by inserting directly.
    today = date.today().isoformat()
    temp_db.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts, completed_ts, sessions_scanned, report) "
        "VALUES (?, ?, ?, 0, 'done')",
        (today, int(time.time()), int(time.time())),
    )
    rc = consolidate.main(["--json"])
    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert result["status"] == "completed"
    assert result["runs"] == [{"status": "already_run", "run_date": today}]


# ---------- catch-up backfill ----------


def _one_session():
    return SessionFile(
        path=Path("/sessions/s.jsonl"),
        session_id="s",
        project_slug="proj",
        modified_ts=1,
        size_bytes=10,
    )


def _ok_agent(report='{"metrics": {"surfaces_evaluated": 2}}'):
    return SimpleNamespace(error=None, report=report, returncode=0)


def test_resolve_dates_yesterday_is_catchup_window():
    dates = consolidate._resolve_dates(SimpleNamespace(date=None, yesterday=True))
    today = date.today()
    expected = [today - timedelta(days=n)
                for n in range(consolidate.CATCHUP_LOOKBACK_DAYS, 0, -1)]
    assert dates == expected
    assert dates[-1] == today - timedelta(days=1)   # ends on yesterday
    assert dates[0] < dates[-1]                      # oldest first


def test_catchup_backfills_only_days_with_sessions(temp_db, monkeypatch, capsys):
    today = date.today()
    gap = (today - timedelta(days=3)).isoformat()

    # Sessions exist only on the 3-days-ago gap day.
    def fake_collect(target_date):
        return [_one_session()] if target_date.isoformat() == gap else []
    monkeypatch.setattr(consolidate, "collect_sessions", fake_collect)

    ran = []

    def fake_agent(*, sessions, db_path, target_date):
        ran.append(target_date)
        return _ok_agent()
    monkeypatch.setattr(consolidate, "run_consolidation_agent", fake_agent)

    rc = consolidate.main(["--yesterday", "--json"])
    assert rc == 0
    assert ran == [gap]                                  # only the day with sessions
    assert consolidate.runs.was_run(temp_db, gap)        # and it was recorded


def test_catchup_skips_already_run_days(temp_db, monkeypatch):
    today = date.today()
    done = (today - timedelta(days=2)).isoformat()
    temp_db.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts, completed_ts, sessions_scanned, report) "
        "VALUES (?, ?, ?, 1, 'done')",
        (done, int(time.time()), int(time.time())),
    )

    monkeypatch.setattr(consolidate, "collect_sessions", lambda d: [_one_session()])

    ran = []

    def fake_agent(*, sessions, db_path, target_date):
        ran.append(target_date)
        return _ok_agent()
    monkeypatch.setattr(consolidate, "run_consolidation_agent", fake_agent)

    consolidate.main(["--yesterday", "--json"])
    assert done not in ran                                # recorded day never re-run


def test_catchup_error_day_is_not_recorded_so_it_retries(temp_db, monkeypatch):
    today = date.today()
    target = (today - timedelta(days=1)).isoformat()

    def fake_collect(target_date):
        return [_one_session()] if target_date.isoformat() == target else []
    monkeypatch.setattr(consolidate, "collect_sessions", fake_collect)

    def fake_agent(*, sessions, db_path, target_date):
        return SimpleNamespace(error="spawn failed", report=None, returncode=1)
    monkeypatch.setattr(consolidate, "run_consolidation_agent", fake_agent)

    rc = consolidate.main(["--yesterday", "--json"])
    assert rc == 1                                        # surfaced as failure
    assert not consolidate.runs.was_run(temp_db, target)  # left un-run → retried next time


def test_date_flag_emits_aggregate_shape(temp_db, monkeypatch, capsys):
    # --date (manual backfill) goes through the same aggregate output as the
    # catch-up sweep — pin the {status, surfaces_cleaned, runs:[...]} shape.
    monkeypatch.setattr(consolidate, "collect_sessions", lambda d: [_one_session()])
    monkeypatch.setattr(consolidate, "run_consolidation_agent",
                        lambda **kw: _ok_agent())

    rc = consolidate.main(["--date", "2026-01-02", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "completed"
    assert "surfaces_cleaned" in out
    assert out["runs"] == [{"status": "completed", "run_date": "2026-01-02",
                            "sessions_scanned": 1, "error": None}]


def test_catchup_skips_when_another_sweep_holds_lock(temp_db, monkeypatch, capsys):
    # A second concurrent sweep must exit cleanly without spawning an agent.
    monkeypatch.setattr(
        consolidate, "collect_sessions",
        lambda d: (_ for _ in ()).throw(AssertionError("ran while lock held")),
    )

    lock_dir = consolidate.db.db_path().parent / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = open(lock_dir / "consolidate.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = consolidate.main(["--yesterday", "--json"])
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"action": "skipped", "reason": "already_running"}
