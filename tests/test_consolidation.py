"""Unit tests for consolidation CLI and session collection."""

from __future__ import annotations

import json
import time
from datetime import date, timezone, datetime
from pathlib import Path

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
    import os
    old_ts = time.time() - 86400 * 5
    os.utime(jsonl, (old_ts, old_ts))

    sessions = collect_sessions(date.today(), projects_dir=tmp_path / "projects")
    assert len(sessions) == 0


# ---------- CLI ----------


def test_consolidate_dry_run(temp_db, monkeypatch, capsys):
    from toolengrams.cli import consolidate
    rc = consolidate.main(["--dry-run", "--json"])
    assert rc == 0


def test_consolidate_idempotent(temp_db, monkeypatch, capsys):
    from toolengrams.cli import consolidate
    # Simulate a previous run by inserting directly.
    temp_db.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts, completed_ts, sessions_scanned, report) "
        "VALUES (?, ?, ?, 0, 'done')",
        (date.today().isoformat(), int(time.time()), int(time.time())),
    )
    rc = consolidate.main(["--json"])
    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert result["status"] == "already_run"
