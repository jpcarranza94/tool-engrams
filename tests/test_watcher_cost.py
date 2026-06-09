"""Per-run cost + token capture: the claude -p JSON envelope's total_cost_usd
and usage land on the SessionResult, flow through the tick onto the
watcher_runs row, and aggregate into the monitor's 24h spend counters."""

from __future__ import annotations

import json
import time

from toolengrams import db
from toolengrams.claude_invoke import ClaudeResult
from toolengrams.cli import monitor
from toolengrams.watcher import SessionResult, agent, log as wlog, runs_store, tick

ENVELOPE = json.dumps({
    "session_id": "w1",
    "result": "ok",
    "total_cost_usd": 0.0231,
    "usage": {
        "input_tokens": 1200,
        "output_tokens": 350,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 400,
    },
})


def _bash_line(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }) + "\n"


def test_session_result_carries_cost_from_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_sandbox_root", lambda: tmp_path)
    monkeypatch.setattr(agent, "invoke_claude_agent",
                        lambda message, **kw: ClaudeResult(stdout=ENVELOPE,
                                                           returncode=0))
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/usr/bin/claude")

    r = agent.run_watcher_session("formation", "p", resume=None,
                                  work_session_id="s1", delta="x")

    assert r.ok and r.watcher_session_id == "w1"
    assert r.cost_usd == 0.0231
    assert r.input_tokens == 1200
    assert r.output_tokens == 350
    assert r.cache_read_tokens == 9000
    assert r.cache_creation_tokens == 400


def test_failed_call_has_no_cost(tmp_path, monkeypatch):
    """No envelope on failure — cost/token fields stay None, never 0."""
    monkeypatch.setattr(agent, "_sandbox_root", lambda: tmp_path)
    monkeypatch.setattr(agent, "invoke_claude_agent",
                        lambda message, **kw: ClaudeResult(stdout="", returncode=1,
                                                           error="exit 1: boom"))
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/usr/bin/claude")

    r = agent.run_watcher_session("formation", "p", resume="sid",
                                  work_session_id="s1", delta="x")

    assert r.ok is False
    assert r.cost_usd is None and r.output_tokens is None


def test_tick_persists_cost_on_run_row(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))

    def runner(role, message, resume, run_id=None, **kw):
        return SessionResult(ok=True, watcher_session_id="w1",
                             cost_usd=0.0231, input_tokens=1200,
                             output_tokens=350, cache_read_tokens=9000,
                             cache_creation_tokens=400)

    monkeypatch.setattr(tick, "CLAUDE_BIN", "claude")
    monkeypatch.setattr(tick, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(wlog, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(tick, "run_watcher_session", runner)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    row = temp_db.execute("SELECT * FROM watcher_runs").fetchone()
    assert row["status"] == "ok"
    assert row["cost_usd"] == 0.0231
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 350
    assert row["cache_read_tokens"] == 9000
    assert row["cache_creation_tokens"] == 400


def test_counts_since_sums_spend_and_snapshot_exposes_it(temp_db):
    now = int(time.time())
    for offset, (cost, tok_out) in enumerate(((0.01, 100), (0.02, 200))):
        run_id = runs_store.start_run(
            temp_db, work_session_id="s", role="formation", pid=1,
            started_ts=now - 10 + offset, model="sonnet", flush=False,
            cursor_from=0, cwd="/c")
        runs_store.finish_run(
            temp_db, run_id, status="ok", ended_ts=now,
            cost_usd=cost, input_tokens=10, output_tokens=tok_out,
            cache_read_tokens=5, cache_creation_tokens=1)
    # An error run with no envelope must not break the sums. Newest, so it is
    # recent_24h[0].
    run_id = runs_store.start_run(
        temp_db, work_session_id="s", role="eval", pid=1,
        started_ts=now, model="sonnet", flush=False, cursor_from=0, cwd="/c")
    runs_store.finish_run(temp_db, run_id, status="error", ended_ts=now,
                          error="timeout")

    c = runs_store.counts_since(temp_db, now - 60)
    assert c["cost_usd"] == 0.03
    assert c["output_tokens"] == 300
    assert c["input_tokens"] == 20
    assert c["cache_read_tokens"] == 10
    assert c["cache_creation_tokens"] == 2

    snap = monitor.build_snapshot(temp_db, now)
    assert snap["counts_24h"]["cost_usd"] == 0.03
    assert snap["recent_24h"][0]["cost_usd"] is None       # the error run, newest
    assert snap["recent_24h"][1]["cost_usd"] == 0.02
