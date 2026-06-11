"""run_tick records a watcher_runs row per model-calling tick, finalizes it
ok/error, skips it on gated ticks, and reaps a prior stale 'running' row when it
takes the lock."""

from __future__ import annotations

import json

from toolengrams import db
from toolengrams.watcher import SessionResult, log as wlog, runs_store, tick


def _bash_line(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }) + "\n"


def _user_line(text: str) -> str:
    return json.dumps(
        {"type": "message", "message": {"role": "user", "content": text}}
    ) + "\n"


def _wire(monkeypatch, tmp_path, runner):
    monkeypatch.setattr(tick, "CLAUDE_BIN", "claude")
    monkeypatch.setattr(tick, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(wlog, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(tick, "run_watcher_session", runner)


def _runs(conn):
    return conn.execute("SELECT * FROM watcher_runs ORDER BY id").fetchall()


def test_model_tick_writes_ok_run_and_passes_run_id(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    seen = {}

    def runner(role, message, run_id=None, **kw):
        seen["run_id"] = run_id
        return SessionResult(ok=True)

    _wire(monkeypatch, tmp_path, runner)
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.run_tick("s", str(transcript), "/cwd")

    rows = _runs(temp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "ok"
    assert r["role"] == "formation"
    assert r["cursor_from"] == 0 and r["cursor_to"] == 1
    assert r["delta_chars"] > 0
    assert r["ended_ts"] is not None
    assert seen["run_id"] == r["id"]   # run id handed to the session for events


def test_failed_tick_writes_error_run(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))

    def runner(role, message, run_id=None, **kw):
        return SessionResult(ok=False,
                             error="claude -p timed out (120s)")

    _wire(monkeypatch, tmp_path, runner)
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.run_tick("s", str(transcript), "/cwd")

    rows = _runs(temp_db)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "timed out" in rows[0]["error"]


def test_gated_tick_writes_no_run(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("hi, how are you"))  # pure chat → gated
    called = []

    def runner(role, message, run_id=None, **kw):
        called.append(role)
        return SessionResult(ok=True)

    _wire(monkeypatch, tmp_path, runner)
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.run_tick("s", str(transcript), "/cwd")

    assert called == []                       # model not called
    assert _runs(temp_db) == []               # and no run row


def test_run_row_committed_before_session_runs(temp_db, tmp_path, monkeypatch):
    """The run row must be committed (visible on another connection) before the
    claude session runs, so the child engram CLI can reference it via run_id."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    seen = {}

    def runner(role, message, run_id=None, **kw):
        with db.session() as conn2:           # a separate connection
            row = conn2.execute(
                "SELECT status FROM watcher_runs WHERE id = ?", (run_id,)
            ).fetchone()
        seen["status"] = row["status"] if row else None
        return SessionResult(ok=True)

    _wire(monkeypatch, tmp_path, runner)
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.run_tick("s", str(transcript), "/cwd")
    assert seen["status"] == "running"        # committed before the session started


def test_reaper_crashes_prior_running_row(temp_db, tmp_path, monkeypatch):
    # A stuck run that never finalized (pid is irrelevant; the lock proves it's dead).
    stale = runs_store.start_run(
        temp_db, work_session_id="s", role="formation", pid=999, started_ts=1,
        model="opus", flush=False, cursor_from=0, cwd="/cwd",
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    _wire(monkeypatch, tmp_path,
          lambda role, message, run_id=None, **kw: SessionResult(True))
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.run_tick("s", str(transcript), "/cwd")

    by_id = {r["id"]: r["status"] for r in _runs(temp_db)}
    assert by_id[stale] == "crashed"          # the old run reaped
    assert "ok" in by_id.values()             # the new run finished cleanly
