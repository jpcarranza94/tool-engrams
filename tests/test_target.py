"""Target adapter contract: registry conformance, selection fallback, and the
claude-code adapter's payload handling."""

from __future__ import annotations

import io
import json

from toolengrams.__main__ import main as engram_main
from toolengrams.target import TARGETS, TargetAdapter, get_target
from toolengrams.target import claude_code
from toolengrams.watcher import state, tick


def test_registry_adapters_satisfy_protocol():
    for name, target in TARGETS.items():
        assert isinstance(target, TargetAdapter), f"{name} missing adapter attrs"
        assert target.NAME == name


def test_get_target_defaults_and_falls_back(capsys):
    assert get_target() is claude_code
    assert get_target("claude-code") is claude_code
    assert get_target("betamax") is claude_code        # unknown → fail-open
    assert "unknown target" in capsys.readouterr().err


def test_transcript_path_prefers_payload():
    payload = {"session_id": "s", "cwd": "/x", "transcript_path": "/given.jsonl"}
    assert claude_code.transcript_path(payload) == "/given.jsonl"


def test_transcript_path_derives_when_missing():
    payload = {"session_id": "sess-1", "cwd": "/Users/x/proj"}
    p = claude_code.transcript_path(payload)
    assert p.endswith("-Users-x-proj/sess-1.jsonl")
    assert ".claude/projects" in p


def test_detect_failure_matrix():
    assert claude_code.detect_failure({"is_error": True})
    assert claude_code.detect_failure({"tool_response": "<error>boom</error>"})
    assert claude_code.detect_failure({"tool_response": "Exit code 1"})
    assert not claude_code.detect_failure({"tool_response": "all good"})
    assert not claude_code.detect_failure({})


def test_format_delta_uses_agent_label():
    line = json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}]}})
    out = claude_code.format_delta([line])
    assert 'AGENT: "hi"' in out
    assert "CLAUDE:" not in out


# ---------- the CLI entry point must stay fail-open on a bad --target ----------


def test_unknown_target_through_main_is_fail_open(monkeypatch, capsys):
    """exit 2 from argparse would be a BLOCKING hook error in Claude Code —
    an unknown --target must degrade through get_target's fallback instead."""
    payload = {"session_id": "s", "cwd": "/tmp",
               "tool_name": "Bash", "tool_input": {"command": "echo hi"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = engram_main(["pretool", "--target", "betamax"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "unknown target" in captured.err
    json.loads(captured.out)            # still emits valid hook JSON


# ---------- target plumbed through the detached tick ----------


def test_spawn_tick_argv_carries_target(monkeypatch):
    argvs = []

    class _P:
        def __init__(self, argv, **kw):
            argvs.append(argv)

    monkeypatch.setattr(tick.subprocess, "Popen", _P)
    tick.spawn_tick("s", "/t.jsonl", "/cwd", role="formation", target="claude-code")
    assert "--target" in argvs[0]
    assert argvs[0][argvs[0].index("--target") + 1] == "claude-code"


def test_tick_main_parses_target(monkeypatch):
    seen = {}
    monkeypatch.setattr(tick, "run_tick",
                        lambda sid, tp, cwd, role="formation", flush=False,
                               target="claude-code": seen.update(target=target) or 0)
    tick.main(["s", "/t", "/cwd", "--role", "eval", "--target", "claude-code"])
    assert seen["target"] == "claude-code"


def test_ensure_row_stores_target_and_heals_transcript_path(temp_db):
    state.ensure_row("s-heal", "", "/cwd", target="claude-code")
    state.ensure_row("s-heal", "/late.jsonl", "/cwd")   # later hook supplies it
    row = temp_db.execute(
        "SELECT transcript_path, target FROM watcher_state "
        "WHERE work_session_id = 's-heal'").fetchone()
    assert row["transcript_path"] == "/late.jsonl"
    assert row["target"] == "claude-code"


def test_sweep_carries_target(temp_db, tmp_path):
    import time
    from toolengrams import db as _db
    f = tmp_path / "t.jsonl"
    f.write_text("{}\n")
    state.ensure_row("s-sweep", str(f), "/cwd", target="claude-code")
    with _db.session() as conn:
        conn.execute(
            "UPDATE watcher_state SET last_tick_ts = ? "
            "WHERE work_session_id = 's-sweep'", (int(time.time()) - 9999,))
    idle = state.sweep_idle(3600)
    assert idle and idle[0].target == "claude-code"
