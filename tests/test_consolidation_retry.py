"""Same-session retry when the consolidation report's JSON envelope is malformed.

These exercise run_consolidation_agent's report-correction loop with a fake
engine, so no real `claude -p` runs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from toolengrams.consolidation import agent, report_parse
from toolengrams.engine import EngineResult
from toolengrams.target.interface import SessionFile

VALID_BLOCK = (
    '```json\n{"metrics": {"quality_score": 0.5, "surfaces_evaluated": 2}, '
    '"recommendations": [{"title": "noisy glob", "severity": "warn"}]}\n```'
)
MALFORMED = "Here is my report. (no JSON block at all)"


def _session(tmp_path):
    return SessionFile(path=tmp_path / "s.jsonl", session_id="s", project_slug="p",
                       modified_ts=1, size_bytes=1024, target="claude-code")


def _fake_engine(monkeypatch, results):
    """Queue of EngineResults; records each EngineRequest it was invoked with."""
    calls = []

    def invoke(req):
        calls.append(req)
        return results[min(len(calls) - 1, len(results) - 1)]

    engine = SimpleNamespace(NAME="fake", is_available=lambda: True,
                             prepare_sandbox=lambda path, spec: None, invoke=invoke)
    monkeypatch.setattr(agent, "get_engine", lambda: engine)
    monkeypatch.setattr(agent, "_get_memory_summary", lambda path: "summary")
    return calls


def test_malformed_then_corrected_on_resume(tmp_path, monkeypatch):
    calls = _fake_engine(monkeypatch, [
        EngineResult(ok=True, returncode=0, text=MALFORMED, session_id="sess-1"),
        EngineResult(ok=True, returncode=0, text=VALID_BLOCK, session_id="sess-1"),
    ])
    res = agent.run_consolidation_agent([_session(tmp_path)], tmp_path / "db", "2026-06-24")

    assert res.returncode == 0 and res.error is None
    # One correction round happened, resuming the primary session.
    assert len(calls) == 2
    assert calls[1].resume_session_id == "sess-1"
    # The stored report now parses cleanly.
    assert report_parse.extract_metrics(res.report)["quality_score"] == 0.5
    assert [r["title"] for r in report_parse.extract_recommendations(res.report)] == ["noisy glob"]


def test_no_session_id_skips_retry(tmp_path, monkeypatch):
    """Ephemeral engines (codex) return no session_id → no retry, lenient parse."""
    calls = _fake_engine(monkeypatch, [
        EngineResult(ok=True, returncode=0, text=MALFORMED, session_id=None),
    ])
    res = agent.run_consolidation_agent([_session(tmp_path)], tmp_path / "db", "2026-06-24")

    assert len(calls) == 1                       # never tried to resume
    assert res.returncode == 0
    assert report_parse.extract_metrics(res.report) == {}   # degraded, not retried


def test_valid_envelope_does_not_retry(tmp_path, monkeypatch):
    calls = _fake_engine(monkeypatch, [
        EngineResult(ok=True, returncode=0, text=VALID_BLOCK, session_id="sess-1"),
    ])
    res = agent.run_consolidation_agent([_session(tmp_path)], tmp_path / "db", "2026-06-24")

    assert len(calls) == 1                       # already valid → no correction
    assert report_parse.extract_metrics(res.report)["quality_score"] == 0.5


def test_retries_are_bounded(tmp_path, monkeypatch):
    """Persistently malformed output stops after MAX_REPORT_RETRIES corrections."""
    calls = _fake_engine(monkeypatch, [
        EngineResult(ok=True, returncode=0, text=MALFORMED, session_id="sess-1"),
    ])
    res = agent.run_consolidation_agent([_session(tmp_path)], tmp_path / "db", "2026-06-24")

    assert len(calls) == 1 + agent.MAX_REPORT_RETRIES   # primary + bounded retries
    assert all(c.resume_session_id == "sess-1" for c in calls[1:])
    assert res.returncode == 0                          # best-effort, not an error


def test_failed_correction_call_does_not_downgrade_run(tmp_path, monkeypatch):
    """A correction call that errors is dropped; the primary's outcome stands."""
    calls = _fake_engine(monkeypatch, [
        EngineResult(ok=True, returncode=0, text=MALFORMED, session_id="sess-1"),
        EngineResult(ok=False, returncode=1, text="", session_id="sess-1"),
    ])
    res = agent.run_consolidation_agent([_session(tmp_path)], tmp_path / "db", "2026-06-24")

    assert len(calls) == 2                       # tried once, then gave up
    assert res.returncode == 0 and res.error is None
    assert report_parse.extract_metrics(res.report) == {}   # kept the lenient parse


def test_merge_preserves_original_prose(tmp_path, monkeypatch):
    """The corrected block is appended; the human-readable prose is kept above."""
    prose = "## Sessions\nReviewed 3 sessions. Found a noisy trigger."
    _fake_engine(monkeypatch, [
        EngineResult(ok=True, returncode=0, text=prose, session_id="sess-1"),
        EngineResult(ok=True, returncode=0, text=VALID_BLOCK, session_id="sess-1"),
    ])
    res = agent.run_consolidation_agent([_session(tmp_path)], tmp_path / "db", "2026-06-24")

    assert "Reviewed 3 sessions" in res.report               # prose survived
    assert report_parse.extract_metrics(res.report)["quality_score"] == 0.5
