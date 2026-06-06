"""Tests for the event-driven watcher tick (gate, coalesce, arm, retry, lock)
across both roles — formation and evaluation.

v10: the model seam is `tick.run_watcher_session(role, message, resume)` →
`SessionResult(ok, watcher_session_id)`. There is no JSON parsing or in-process
save; the watcher session calls the engram CLI itself.
"""

from __future__ import annotations

import json
import time

from toolengrams import db
from toolengrams.watcher import SessionResult, log as wlog, state, tick


# ---------- transcript + runner helpers ----------


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


def _ok(sid="w1"):
    """A runner that always succeeds, returning session id `sid`."""
    def _runner(role, message, resume):
        return SessionResult(ok=True, watcher_session_id=sid)
    return _runner


def _wire(monkeypatch, tmp_path, runner):
    monkeypatch.setattr(tick, "CLAUDE_BIN", "claude")
    monkeypatch.setattr(tick, "LOG_PATH", tmp_path / "watcher.log")   # lock dir parent
    monkeypatch.setattr(wlog, "LOG_PATH", tmp_path / "watcher.log")   # _log sink
    monkeypatch.setattr(tick, "run_watcher_session", runner)


def _col(session_id, col, role="formation"):
    with db.session() as conn:
        row = conn.execute(
            f"SELECT {col} FROM watcher_state WHERE work_session_id = ? AND role = ?",
            (session_id, role),
        ).fetchone()
    return row[col] if row else None


def _seed_pending_surface(session_id, *, name="m", body="b", kind="hint"):
    """A memory plus an unjudged surface in this session (eval's input)."""
    with db.session() as conn:
        cur = conn.execute(
            "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
            "VALUES (?, '', ?, ?, 'global', NULL, ?)",
            (name, body, kind, int(time.time())),
        )
        mid = cur.lastrowid
        conn.execute(
            "INSERT INTO session_surfaces "
            "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface, first_token) "
            "VALUES (?, ?, ?, 'pre_tool_use', 'tu-1', 1, 'git')",
            (session_id, mid, int(time.time())),
        )
    return mid


# ---------- coalesce ----------


def test_should_spawn_coalesce(temp_db, monkeypatch):
    monkeypatch.delenv("ENGRAM_TICK_COALESCE_SEC", raising=False)
    tick.ensure_row("s", "/t.jsonl", "/cwd")
    assert tick.should_spawn("s", flush=False) is True  # last_tick_ts=0
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_tick_ts = ? "
            "WHERE work_session_id = 's' AND role = 'formation'",
            (int(time.time()),),
        )
    assert tick.should_spawn("s", flush=False) is False  # within interval
    assert tick.should_spawn("s", flush=True) is True     # flush ignores coalesce


def test_coalesce_is_per_role(temp_db, monkeypatch):
    """A recent formation tick does not coalesce away an eval tick."""
    monkeypatch.delenv("ENGRAM_TICK_COALESCE_SEC", raising=False)
    tick.ensure_row("s", "/t.jsonl", "/cwd", role="formation")
    tick.ensure_row("s", "/t.jsonl", "/cwd", role="eval")
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_tick_ts = ? "
            "WHERE work_session_id = 's' AND role = 'formation'",
            (int(time.time()),),
        )
    assert tick.should_spawn("s", flush=False, role="formation") is False
    assert tick.should_spawn("s", flush=False, role="eval") is True  # eval untouched


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


# ---------- formation run_tick gate ----------


def test_run_tick_gate_skips_pure_chat(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("hello there, how are you"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: calls.append(role) or SessionResult(True, "w1"))
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert calls == []                        # no model call on a pure-chat turn
    assert _col("s", "last_line_read") == 1   # but cursor advanced past it


def test_run_tick_armed_forces_model_on_chat(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("ok thanks"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: calls.append(role) or SessionResult(True, "w1"))
    tick.ensure_row("s", str(transcript), "/cwd")
    tick.arm("s")

    tick.run_tick("s", str(transcript), "/cwd")

    assert calls == ["formation"]     # armed upgraded the skippable turn into a call
    assert _col("s", "armed") == 0    # armed consumed


def test_run_tick_calls_model_on_tool_activity(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create --title x"))
    seen = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: seen.append((role, resume)) or SessionResult(True, "w1"))
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert seen == [("formation", None)]      # formation session, fresh (no resume)
    assert _col("s", "last_line_read") == 1
    assert _col("s", "watcher_session_id") == "w1"


def test_run_tick_holds_cursor_and_persists_streak_on_failure(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: SessionResult(ok=False, watcher_session_id=resume))
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert _col("s", "last_line_read") == 0   # held for retry
    assert _col("s", "fail_streak") == 1      # streak persisted across events


def test_run_tick_lock_prevents_concurrent_processing(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: calls.append(role) or SessionResult(True, "w1"))
    tick.ensure_row("s", str(transcript), "/cwd")

    # Hold the per-(session, role) lock, then a second tick must no-op.
    with tick._tick_lock("s", "formation") as got:
        assert got is True
        tick.run_tick("s", str(transcript), "/cwd")

    assert calls == []                        # couldn't acquire lock → no work
    assert _col("s", "last_line_read") == 0   # cursor untouched


# ---------- cross-event retry (fail_streak persisted across tick processes) ----------


def test_run_tick_holds_then_gives_up_across_events(temp_db, tmp_path, monkeypatch):
    """A persistently failing window is retried in place (cursor HELD) across
    independent tick events, and only advances after MAX_FORM_RETRIES."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("gh pr create"))
    calls = {"n": 0}

    def boom(role, msg, resume):
        calls["n"] += 1
        return SessionResult(ok=False, watcher_session_id=resume)

    _wire(monkeypatch, tmp_path, boom)
    tick.ensure_row("s", str(transcript), "/cwd")

    for _ in range(tick.MAX_FORM_RETRIES):
        tick.run_tick("s", str(transcript), "/cwd")

    assert calls["n"] == tick.MAX_FORM_RETRIES   # same window retried MAX times
    assert _col("s", "last_line_read") == 1      # then advanced past it (gave up)
    assert _col("s", "fail_streak") == 0         # streak reset after give-up


def test_run_tick_resets_session_on_failure_across_events(temp_db, tmp_path, monkeypatch):
    """After a failure on a --resume window, the next event must retry via a
    FRESH session (resume=None), not re-feed the bad turn via resume."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("first window"))
    route = []
    script = iter([True, False, True])  # window1 ok, window2 fails, window2 retried ok

    def fake(role, msg, resume):
        route.append(resume)
        ok = next(script)
        return SessionResult(ok=ok, watcher_session_id="w1" if ok else resume)

    _wire(monkeypatch, tmp_path, fake)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")        # event 1: window1, fresh → ok
    with open(transcript, "a") as f:                   # window2 arrives
        f.write(_bash_line("second window"))
    tick.run_tick("s", str(transcript), "/cwd")        # event 2: window2 via resume → fail
    tick.run_tick("s", str(transcript), "/cwd")        # event 3: window2 via fresh → ok

    assert route == [None, "w1", None]        # new, resume, new
    assert _col("s", "last_line_read") == 2   # both windows ultimately consumed


# ---------- eval run_tick ----------


def test_eval_tick_skips_when_no_pending(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("git push"))
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: calls.append(role) or SessionResult(True, "e1"))
    tick.ensure_row("s", str(transcript), "/cwd", role="eval")

    tick.run_tick("s", str(transcript), "/cwd", role="eval")

    assert calls == []                                       # nothing to judge
    assert _col("s", "last_line_read", role="eval") == 1     # advanced past evidence


def test_eval_tick_runs_when_pending(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("git push --force-with-lease"))
    _seed_pending_surface("s")
    seen = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: seen.append((role, resume)) or SessionResult(True, "e1"))
    tick.ensure_row("s", str(transcript), "/cwd", role="eval")

    tick.run_tick("s", str(transcript), "/cwd", role="eval")

    assert seen == [("eval", None)]                          # eval session ran
    assert _col("s", "last_line_read", role="eval") == 1
    assert _col("s", "watcher_session_id", role="eval") == "e1"


def test_eval_tick_defers_when_no_new_evidence(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")  # no lines yet
    _seed_pending_surface("s")
    calls = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: calls.append(role) or SessionResult(True, "e1"))
    tick.ensure_row("s", str(transcript), "/cwd", role="eval")

    tick.run_tick("s", str(transcript), "/cwd", role="eval")

    assert calls == []                                        # deferred — no evidence
    assert _col("s", "last_line_read", role="eval") == 0      # cursor held


def test_eval_tick_flush_forces_run_without_new_lines(temp_db, tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")  # no new lines
    _seed_pending_surface("s")
    seen = []
    _wire(monkeypatch, tmp_path,
          lambda role, msg, resume: seen.append(role) or SessionResult(True, "e1"))
    tick.ensure_row("s", str(transcript), "/cwd", role="eval")

    tick.run_tick("s", str(transcript), "/cwd", role="eval", flush=True)

    assert seen == ["eval"]   # final pass forces closure even with no new evidence


# ---------- trigger_eval gating ----------


def test_trigger_eval_skips_without_pending(temp_db, tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(tick, "spawn_tick",
                        lambda sid, tp, cwd, flush=False, role="formation": spawned.append(role))
    tick.trigger_eval("s", "/t.jsonl", "/cwd", reason="stop")
    assert spawned == []


def test_trigger_eval_spawns_with_pending(temp_db, tmp_path, monkeypatch):
    _seed_pending_surface("s")
    spawned = []
    monkeypatch.setattr(tick, "spawn_tick",
                        lambda sid, tp, cwd, flush=False, role="formation": spawned.append(role))
    tick.trigger_eval("s", "/t.jsonl", "/cwd", reason="stop")
    assert spawned == ["eval"]


# ---------- idle sweep (tail recovery from SessionStart) ----------


def test_sweep_idle_sessions_fires_flush_tick(temp_db, tmp_path, monkeypatch):
    """An abandoned session (old last tick + unread lines) gets a formation flush
    tick re-fired; the current session is excluded."""
    f = tmp_path / "abandoned.jsonl"
    f.write_text(_bash_line("a") + _bash_line("b"))
    state.ensure_row("abandoned", str(f), "/cwd")
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_line_read = 0, last_tick_ts = ? "
            "WHERE work_session_id = 'abandoned' AND role = 'formation'",
            (int(time.time()) - 3600,),
        )

    spawned = []
    monkeypatch.setattr(tick, "spawn_tick",
                        lambda sid, tp, cwd, flush=False, role="formation": spawned.append((sid, flush, role)))

    n = tick.sweep_idle_sessions("current-session")

    assert n == 1
    # No pending surfaces → only the formation flush fires (eval self-gates out).
    assert spawned == [("abandoned", True, "formation")]


def test_sweep_does_not_refire_after_tail_processed(temp_db, tmp_path, monkeypatch):
    """The flush tick the sweep fires consumes the tail (advances the cursor and
    bumps last_tick_ts), so the session drops out of the next sweep."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("a") + _bash_line("b"))
    _wire(monkeypatch, tmp_path, _ok())
    state.ensure_row("s", str(transcript), "/cwd")
    with db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_tick_ts = ? "
            "WHERE work_session_id = 's' AND role = 'formation'",
            (int(time.time()) - 3600,),
        )
    assert [x.session_id for x in state.sweep_idle(idle_sec=1800)] == ["s"]

    # The re-fired flush tick runs and consumes the tail.
    tick.run_tick("s", str(transcript), "/cwd", flush=True)

    assert _col("s", "last_line_read") == 2          # tail consumed
    assert state.sweep_idle(idle_sec=1800) == []     # no longer a lost tail
