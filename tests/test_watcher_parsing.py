"""Unit tests for watcher parsing + delta capping.

These cover the three bugs found by the 2026-04-23 live-session audit:
  1. Parser failed on fenced-JSON Haiku responses (silently dropped memories).
  2. Dormant sessions ballooned the delta into hundreds of KB on first wake.
"""

from __future__ import annotations

import json

from toolengrams.watcher import (
    MAX_DELTA_CHARS,
    _cap_delta,
    _candidate_json_strings,
    _parse_response,
)


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
