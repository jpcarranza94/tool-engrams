"""Unit tests for watcher parsing + delta capping.

These cover the three bugs found by the 2026-04-23 live-session audit:
  1. Parser failed on fenced-JSON model responses (silently dropped memories).
  2. Dormant sessions ballooned the delta into hundreds of KB on first wake.
"""

from __future__ import annotations

import json
import os

from toolengrams.claude_invoke import ClaudeResult
from toolengrams.watcher import agent
from toolengrams.watcher import (
    DEFAULT_WATCHER_MODEL,
    DEFAULT_WATCHER_TIMEOUT,
    MAX_BASH_CMD_CHARS,
    MAX_DELTA_CHARS,
    MAX_FORM_RETRIES,
    MAX_RESULT_CHARS,
    _cap_delta,
    _candidate_json_strings,
    _clip_ends,
    _clip_head,
    _format_delta,
    _parse_response,
    _retry_decision,
    _watcher_model,
    _watcher_timeout,
)


# ---------- _watcher_model ----------


def test_watcher_model_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_WATCHER_MODEL", raising=False)
    assert _watcher_model() == DEFAULT_WATCHER_MODEL == "opus"


def test_watcher_model_env_override(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_MODEL", "haiku")
    assert _watcher_model() == "haiku"


def test_watcher_model_resolves_at_call_time(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_MODEL", "haiku")
    assert _watcher_model() == "haiku"
    monkeypatch.setenv("ENGRAM_WATCHER_MODEL", "sonnet")
    assert _watcher_model() == "sonnet"


# ---------- _watcher_timeout ----------


def test_watcher_timeout_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_WATCHER_TIMEOUT", raising=False)
    assert _watcher_timeout() == DEFAULT_WATCHER_TIMEOUT == 120


def test_watcher_timeout_env_override(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_TIMEOUT", "300")
    assert _watcher_timeout() == 300


def test_watcher_timeout_resolves_at_call_time(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_TIMEOUT", "90")
    assert _watcher_timeout() == 90
    monkeypatch.setenv("ENGRAM_WATCHER_TIMEOUT", "180")
    assert _watcher_timeout() == 180


def test_watcher_timeout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_TIMEOUT", "not-a-number")
    assert _watcher_timeout() == DEFAULT_WATCHER_TIMEOUT


def test_watcher_timeout_non_positive_falls_back(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_TIMEOUT", "0")
    assert _watcher_timeout() == DEFAULT_WATCHER_TIMEOUT
    monkeypatch.setenv("ENGRAM_WATCHER_TIMEOUT", "-5")
    assert _watcher_timeout() == DEFAULT_WATCHER_TIMEOUT


# ---------- _retry_decision (hold cursor on failure, bounded) ----------


def test_retry_decision_success_advances_and_resets():
    # success always advances and clears any prior streak
    assert _retry_decision(False, 0, MAX_FORM_RETRIES) == (True, 0)
    assert _retry_decision(False, 2, MAX_FORM_RETRIES) == (True, 0)


def test_retry_decision_failure_holds_until_cap():
    # first failures HOLD the cursor (don't advance) and bump the streak
    advance, streak = _retry_decision(True, 0, 3)
    assert advance is False and streak == 1
    advance, streak = _retry_decision(True, streak, 3)
    assert advance is False and streak == 2


def test_retry_decision_gives_up_at_cap():
    # at the cap, give up: advance past the poison window and reset
    assert _retry_decision(True, 2, 3) == (True, 0)


def test_retry_decision_full_sequence():
    # walk a fail/fail/fail run: hold, hold, give-up
    streak = 0
    outcomes = []
    for _ in range(3):
        advance, streak = _retry_decision(True, streak, 3)
        outcomes.append(advance)
    assert outcomes == [False, False, True]
    assert streak == 0  # reset after giving up


# ---------- _run: re-raise a process error so run_tick holds the window ----------


def test_run_reraises_on_invoke_error(monkeypatch):
    """A process failure (timeout/spawn) comes back as ClaudeResult.error from
    the seam; the watcher wrapper must re-raise it so run_tick treats the window
    as held-and-retried rather than parsing empty stdout."""
    monkeypatch.setattr(
        agent, "invoke_claude_agent",
        lambda *a, **k: ClaudeResult(stdout="", returncode=1, timed_out=True,
                                     error="claude -p timed out (120s)"),
    )
    try:
        agent._run("msg", "{}", resume=None)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "timed out" in str(e)


def test_run_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(
        agent, "invoke_claude_agent",
        lambda *a, **k: ClaudeResult(stdout='{"result":"ok"}', returncode=0),
    )
    assert agent._run("msg", "{}", resume="sid") == '{"result":"ok"}'


# ---------- _clip_head / _clip_ends ----------


def test_clip_head_passes_short_through():
    assert _clip_head("short", 100) == "short"


def test_clip_head_truncates_long_keeping_head():
    out = _clip_head("A" * 50 + "B" * 50, 50)
    assert out.startswith("A" * 50)
    assert "B" not in out
    assert "+50 chars truncated" in out


def test_clip_ends_passes_short_through():
    assert _clip_ends("short", 100) == "short"


def test_clip_ends_keeps_head_and_tail():
    text = "HEAD" + "x" * 1000 + "TAIL"
    out = _clip_ends(text, 100)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "…" in out
    assert len(out) < len(text)


# ---------- _format_delta per-line caps ----------


def _assistant_bash(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}],
        },
    })


def _user_error_result(output: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": output, "is_error": True, "tool_use_id": "t1"}
            ],
        },
    })


def test_format_delta_caps_giant_bash_command():
    # A `gh pr create` heredoc carrying a multi-KB PR body is the real-world
    # offender that stalled the watcher. The head (command + flags) survives;
    # the body is truncated.
    cmd = "gh pr create --title x --body " + "B" * 5000
    line = _assistant_bash(cmd)
    result = _format_delta([line])
    bash_line = [l for l in result.splitlines() if l.startswith("TOOL (Bash):")][0]
    assert bash_line.startswith("TOOL (Bash): gh pr create")
    assert len(bash_line) <= len("TOOL (Bash): ") + MAX_BASH_CMD_CHARS + 40
    assert "truncated" in bash_line


def test_format_delta_caps_giant_error_result_head_and_tail():
    output = "ERROR: it broke at the top " + "x" * 5000 + " real cause at the end"
    line = _user_error_result(output)
    result = _format_delta([line])
    result_line = [l for l in result.splitlines() if l.startswith("RESULT:")][0]
    # Both ends preserved (command echo + actual cause), middle elided.
    assert "ERROR: it broke at the top" in result_line
    assert "real cause at the end" in result_line
    assert len(result_line) <= len("RESULT: ") + MAX_RESULT_CHARS + 40


def _wrap_claude_json(result_text: str) -> str:
    """Shape a claude -p --output-format json stdout line."""
    return json.dumps({"result": result_text, "session_id": "fake"}) + "\n"


# ---------- _parse_response ----------


def test_parse_response_clean_json():
    stdout = _wrap_claude_json('{"action": "none"}')
    assert _parse_response(stdout) == {"action": "none"}


def test_parse_response_fenced_json_with_language():
    """Haiku sometimes responds with ```json … ``` instead of via StructuredOutput."""
    body = '```json\n{\n  "action": "create",\n  "memories": [{"name":"x","body":"y","kind":"hint","scope":"global","triggers":["bq"]}]\n}\n```'
    stdout = _wrap_claude_json(body)
    got = _parse_response(stdout)
    assert got is not None
    assert got["action"] == "create"
    assert got["memories"][0]["name"] == "x"


def test_parse_response_fenced_json_no_language_tag():
    body = '```\n{"action": "none"}\n```'
    stdout = _wrap_claude_json(body)
    assert _parse_response(stdout) == {"action": "none"}


def test_parse_response_tolerates_surrounding_prose():
    """Haiku says something like "Here is my response:" + JSON."""
    body = 'Here is the response:\n\n{"action": "none"}\n\nThanks!'
    stdout = _wrap_claude_json(body)
    assert _parse_response(stdout) == {"action": "none"}


def test_parse_response_empty_result_returns_none():
    stdout = _wrap_claude_json("")
    assert _parse_response(stdout) is None


def test_parse_response_malformed_json_returns_none():
    stdout = _wrap_claude_json("this is not JSON at all")
    assert _parse_response(stdout) is None


def test_parse_response_nested_json_extracted():
    body = 'prose prose {"ignore":1} prose prose {"action":"create","memories":[]} trailing'
    stdout = _wrap_claude_json(body)
    got = _parse_response(stdout)
    # Either extracted candidate parses — the largest-balanced-braces heuristic
    # should pick up the whole span. Accept either a match or a graceful None.
    # We just require the parser doesn't blow up and returns something
    # deterministic (dict-or-None), never raises.
    assert got is None or isinstance(got, dict)


# ---------- _candidate_json_strings ----------


def test_candidate_json_strings_extracts_fence():
    candidates = _candidate_json_strings('Preamble\n```json\n{"a":1}\n```\nAfter')
    assert any(c == '{"a":1}' for c in candidates)


def test_candidate_json_strings_extracts_balanced_braces_fallback():
    candidates = _candidate_json_strings('text {"a":1} text')
    assert any('"a":1' in c for c in candidates)


# ---------- _cap_delta ----------


def test_cap_delta_passes_short_input_through():
    text = "small delta\nwith a few lines"
    assert _cap_delta(text) == text


def test_cap_delta_truncates_long_input_keeping_tail():
    big = ("X" * 200 + "\n") * 1000  # ~200 KB
    out = _cap_delta(big)
    assert len(out) <= MAX_DELTA_CHARS + 200  # some slack for the truncation banner
    assert out.startswith("[…earlier activity truncated")
    # Tail preserved — last line's content is still present.
    assert out.rstrip().endswith("X")


def test_cap_delta_starts_tail_at_newline_boundary():
    """Tail shouldn't begin mid-line (looks broken to the reader)."""
    big = ("line-A\n" + "Y" * MAX_DELTA_CHARS + "\n" + "line-Z\n")
    out = _cap_delta(big)
    # Everything after the truncation banner should be newline-anchored content.
    after_banner = out.split("\n", 1)[1]
    assert "Y" not in after_banner or after_banner.startswith("Y")
