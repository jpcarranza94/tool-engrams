"""Tests for the event-driven watcher tick (gate, coalesce, arm, retry, lock)."""

from __future__ import annotations

import json
import time

from toolengrams import db
from toolengrams.watcher import lifecycle, tick


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
    monkeypatch.setattr(tick, "LOG_PATH", tmp_path / "watcher.log")     # lock dir
    monkeypatch.setattr(lifecycle, "LOG_PATH", tmp_path / "watcher.log")  # _log sink
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
