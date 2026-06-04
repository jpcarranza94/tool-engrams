"""Tests for the event-driven watcher tick (gate, coalesce, arm, retry, lock)."""

from __future__ import annotations

import json
import time

from toolengrams import db
from toolengrams.watcher import log as wlog, state, tick


# ---------- transcript + claude -p stdout helpers ----------


def _user_line(text: str) -> str:
    return json.dumps(
        {"type": "message", "message": {"role": "user", "content": text}}
    ) + "\n"


def _bash_line(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }) + "\n"


_OK_NONE = json.dumps({"structured_output": {"action": "none"}, "session_id": "w1"})
_OK_CREATE = json.dumps({
    "structured_output": {"action": "create", "memories": [
        {"name": "mem-x", "body": "Without this memory...", "kind": "hint",
         "scope": "global", "triggers": ["gh pr create"]}
    ]},
    "session_id": "w1",
})


def _wire(monkeypatch, tmp_path, new_fn, resume_fn):
    monkeypatch.setattr(tick, "CLAUDE_BIN", "claude")
    monkeypatch.setattr(tick, "LOG_PATH", tmp_path / "watcher.log")   # lock dir parent
    monkeypatch.setattr(wlog, "LOG_PATH", tmp_path / "watcher.log")   # _log sink
    monkeypatch.setattr(tick, "_claude_p_new", new_fn)
    monkeypatch.setattr(tick, "_claude_p_resume", resume_fn)


def _col(session_id, col):
    with db.session() as conn:
        row = conn.execute(
            f"SELECT {col} FROM watcher_state WHERE work_session_id = ?",
            (session_id,),
        ).fetchone()
    return row[col] if row else None


# ---------- coalesce ----------


def test_should_spawn_coalesce(temp_db, monkeypatch):
    monkeypatch.delenv("ENGRAM_TICK_COALESCE_SEC", raising=False)
    tick.ensure_row("s", "/t.jsonl", "/cwd")
    assert tick.should_spawn("s", flush=False) is True  # last_tick_ts=0
    with db.session() as conn:
        conn.execute("UPDATE watcher_state SET last_tick_ts = ? WHERE work_session_id = 's'",
                     (int(time.time()),))
    assert tick.should_spawn("s", flush=False) is False  # within interval
    assert tick.should_spawn("s", flush=True) is True     # flush ignores coalesce


def test_coalesce_env_override(monkeypatch):
    monkeypatch.setenv("ENGRAM_TICK_COALESCE_SEC", "0")
    assert tick._coalesce_sec() == 0
    monkeypatch.setenv("ENGRAM_TICK_COALESCE_SEC", "junk")
    assert tick._coalesce_sec() == tick.DEFAULT_TICK_COALESCE_SEC


# ---------- arm ----------


def test_arm_sets_flag(temp_db):
    tick.ensure_row("s", "/t.jsonl", "/cwd")
    tick.arm("s")
    assert _col("s", "armed") == 1


# ---------- run_tick gate ----------


def test_run_tick_gate_skips_pure_chat(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("hello there, how are you"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda *a: calls.append("new") or _OK_NONE,
          lambda *a: calls.append("resume") or _OK_NONE)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert calls == []           # no model call on a pure-chat turn
    assert _col("s", "last_line_read") == 1   # but cursor advanced past it


def test_run_tick_armed_forces_model_on_chat(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("ok thanks"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda *a: calls.append("new") or _OK_NONE,
          lambda *a: calls.append("resume") or _OK_NONE)
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.arm("s")

    tick.run_tick("s", str(transcript), "/cwd")

    assert calls == ["new"]      # armed upgraded the skippable turn into a call
    assert _col("s", "armed") == 0   # armed consumed


def test_run_tick_saves_on_tool_activity(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create --title x"))
    saved = []
    _wire(monkeypatch, tmp_path, lambda *a: _OK_CREATE, lambda *a: _OK_CREATE)
    monkeypatch.setattr(tick, "_save_memory", lambda mem, cwd: saved.append(mem["name"]))
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert saved == ["mem-x"]
    assert _col("s", "last_line_read") == 1


def test_run_tick_holds_cursor_and_persists_streak_on_failure(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))

    def boom(*a):
        raise RuntimeError("model down")

    _wire(monkeypatch, tmp_path, boom, boom)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert _col("s", "last_line_read") == 0   # held for retry
    assert _col("s", "fail_streak") == 1      # streak persisted across events


def test_run_tick_lock_prevents_concurrent_processing(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda *a: calls.append("new") or _OK_NONE,
          lambda *a: calls.append("resume") or _OK_NONE)
    tick.ensure_row("s", str(transcript), "/cwd")

    # Hold the per-session lock, then a second tick must no-op.
    with tick._tick_lock("s") as got:
        assert got is True
        tick.run_tick("s", str(transcript), "/cwd")

    assert calls == []                        # couldn't acquire lock → no work
    assert _col("s", "last_line_read") == 0   # cursor untouched


# ---------- cross-event retry (fail_streak persisted across tick processes) ----------


# A claude -p envelope that parses as conversational prose → parse_error.
_JUNK_PROSE = json.dumps({"result": "Sure! Happy to help.", "session_id": "w1"})


def test_run_tick_holds_then_gives_up_across_events(temp_db, tmp_path, monkeypatch):
    """A persistently failing window is retried in place (cursor HELD) across
    independent tick events, and only advances after MAX_FORM_RETRIES — proving
    fail_streak carries through watcher_state, not process memory."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    calls = {"n": 0}

    def boom(*a):
        calls["n"] += 1
        raise RuntimeError("model down")

    _wire(monkeypatch, tmp_path, boom, boom)
    tick.ensure_row("s", str(transcript), "/cwd")

    # Each run_tick is one event (one fresh process in production).
    for _ in range(tick.MAX_FORM_RETRIES):
        tick.run_tick("s", str(transcript), "/cwd")

    assert calls["n"] == tick.MAX_FORM_RETRIES   # same window retried MAX times
    assert _col("s", "last_line_read") == 1      # then advanced past it (gave up)
    assert _col("s", "fail_streak") == 0         # streak reset after give-up


def test_run_tick_resets_session_on_parse_failure_across_events(temp_db, tmp_path, monkeypatch):
    """After a parse failure on a --resume window, the next event must retry via
    a FRESH session (_claude_p_new), not re-feed the bad turn via resume."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("first window"))
    route = []

    def fake_new(*a):
        route.append("new")
        return _OK_NONE          # success → advance, sets watcher_session_id w1

    def fake_resume(*a):
        route.append("resume")
        return _JUNK_PROSE       # parse_error → hold + reset session

    _wire(monkeypatch, tmp_path, fake_new, fake_resume)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")                 # event 1: window1 via new
    with open(transcript, "a") as f:                            # window2 arrives
        f.write(_bash_line("second window"))
    tick.run_tick("s", str(transcript), "/cwd")                 # event 2: window2 via resume (parse-fail)
    tick.run_tick("s", str(transcript), "/cwd")                 # event 3: window2 retried via new

    assert route == ["new", "resume", "new"]
    assert _col("s", "last_line_read") == 2   # both windows ultimately consumed


# ---------- idle sweep (tail recovery from SessionStart) ----------


def test_sweep_idle_sessions_fires_flush_tick(temp_db, tmp_path, monkeypatch):
    """An abandoned session (old last tick + unread lines) gets a flush tick
    re-fired; the current session is excluded."""
    f = tmp_path / "abandoned.jsonl"
    f.write_text(_bash_line("a") + _bash_line("b"))
    state.ensure_row("abandoned", str(f), "/cwd")
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_line_read = 0, last_tick_ts = ? "
            "WHERE work_session_id = 'abandoned'",
            (int(time.time()) - 3600,),
        )

    spawned = []
    monkeypatch.setattr(tick, "spawn_tick",
                        lambda sid, tp, cwd, flush=False: spawned.append((sid, flush)))

    n = tick.sweep_idle_sessions("current-session")

    assert n == 1
    assert spawned == [("abandoned", True)]   # flush tick for the abandoned tail
