"""Unit tests for consolidation CLI and session collection."""

from __future__ import annotations

import json
import os
import time
from datetime import date
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
    temp_db.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts, completed_ts, sessions_scanned, report) "
        "VALUES (?, ?, ?, 0, 'done')",
        (date.today().isoformat(), int(time.time()), int(time.time())),
    )
    rc = consolidate.main(["--json"])
    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert result["status"] == "already_run"
