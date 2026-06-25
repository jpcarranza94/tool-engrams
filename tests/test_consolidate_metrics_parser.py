"""_extract_metrics picks up the new memories_verified field."""

from __future__ import annotations

from toolengrams.consolidation.report_parse import extract_metrics as _extract_metrics


def test_metrics_block_extracts_all_fields():
    report = """
    Some prose ...

    ```json
    {
      "metrics": {
        "sessions_reviewed": 5,
        "surfaces_evaluated": 12,
        "surfaces_helpful": 9,
        "surfaces_noise": 1,
        "surfaces_neutral": 2,
        "memories_created": 1,
        "memories_pruned": 2,
        "memories_verified": 7,
        "total_active_after": 160,
        "quality_score": 0.75
      }
    }
    ```
    """
    m = _extract_metrics(report)
    assert m["memories_verified"] == 7
    assert m["memories_pruned"] == 2
    assert m["quality_score"] == 0.75


def test_missing_memories_verified_returns_empty():
    """Backward-compat: old agents that don't emit memories_verified still parse."""
    report = """
    ```json
    {"metrics": {"sessions_reviewed": 1, "memories_pruned": 0}}
    ```
    """
    m = _extract_metrics(report)
    assert m.get("memories_verified") is None
    assert m.get("memories_pruned") == 0
