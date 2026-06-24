"""_extract_recommendations parses + validates the recommendations array, and
metrics still parse from the same envelope (issue #64)."""

from __future__ import annotations

from toolengrams.cli.consolidate import _extract_metrics, _extract_recommendations


_ENVELOPE = """
Prose report here ...

```json
{
  "metrics": {"surfaces_evaluated": 4, "surfaces_helpful": 3, "quality_score": 0.75},
  "recommendations": [
    {"title": "path-glob read-vs-write noise", "severity": "warn",
     "status": "open", "detail": "fires on reads"},
    {"title": "stale deploy memory", "severity": "critical", "status": "done"}
  ]
}
```
"""


def test_metrics_and_recommendations_coexist():
    """One envelope yields both — metrics parsing is unbroken by the sibling key."""
    assert _extract_metrics(_ENVELOPE)["quality_score"] == 0.75
    recs = _extract_recommendations(_ENVELOPE)
    assert len(recs) == 2
    assert recs[0] == {
        "title": "path-glob read-vs-write noise", "severity": "warn",
        "status": "open", "detail": "fires on reads", "issue_url": None,
    }
    assert recs[1]["status"] == "done"
    assert recs[1]["detail"] is None


def test_missing_recommendations_key_returns_empty():
    report = '```json\n{"metrics": {"quality_score": 1.0}}\n```'
    assert _extract_recommendations(report) == []


def test_no_json_block_returns_empty():
    assert _extract_recommendations("just prose, no block") == []


def test_invalid_severity_and_status_coerced_to_defaults():
    report = """```json
{"recommendations": [{"title": "x", "severity": "URGENT", "status": "wontfix"}]}
```"""
    rec = _extract_recommendations(report)[0]
    assert rec["severity"] == "info"
    assert rec["status"] == "open"


def test_entry_without_title_is_dropped():
    report = """```json
{"recommendations": [
  {"severity": "warn", "detail": "no title"},
  {"title": "   ", "detail": "blank title"},
  {"title": "kept"}
]}
```"""
    titles = [r["title"] for r in _extract_recommendations(report)]
    assert titles == ["kept"]


def test_non_dict_entries_skipped():
    report = '```json\n{"recommendations": ["a string", 42, {"title": "real"}]}\n```'
    assert [r["title"] for r in _extract_recommendations(report)] == ["real"]


def test_recommendations_not_a_list_returns_empty():
    report = '```json\n{"recommendations": {"title": "should be in a list"}}\n```'
    assert _extract_recommendations(report) == []


def test_issue_url_and_detail_normalized():
    report = """```json
{"recommendations": [{"title": "t", "detail": "  spaced  ",
 "issue_url": "https://example.com/1"}]}
```"""
    rec = _extract_recommendations(report)[0]
    assert rec["detail"] == "spaced"
    assert rec["issue_url"] == "https://example.com/1"
