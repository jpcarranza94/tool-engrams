"""The consolidate recorder maps the agent envelope onto the RIGHT columns
(WS4.1 + WS4.3): memories_archived reads the distinct `memories_archived` key
(not `memories_created`), memories_discovered stays = memories_created, and the
revived memories_strengthened passes through instead of a hardcoded 0.
"""

from __future__ import annotations

from types import SimpleNamespace

from toolengrams.cli import consolidate
from toolengrams.consolidation import runs


_ENVELOPE = """
Prose report ...

```json
{
  "metrics": {
    "surfaces_evaluated": 10,
    "surfaces_helpful": 7,
    "surfaces_noise": 1,
    "memories_created": 3,
    "memories_pruned": 5,
    "memories_archived": 8,
    "memories_strengthened": 4,
    "memories_verified": 2,
    "quality_score": 0.7
  }
}
```
"""


def _drive(temp_db, monkeypatch, report):
    monkeypatch.setattr(
        consolidate, "collect_sessions",
        lambda d: [SimpleNamespace(session_id="s", size_bytes=1, target="claude-code")])
    monkeypatch.setattr(
        consolidate, "run_consolidation_agent",
        lambda **kw: SimpleNamespace(error=None, report=report, returncode=0))
    rc = consolidate.main(["--date", "2026-07-01", "--json"])
    assert rc == 0
    return runs.last_run(temp_db)


def test_archived_reads_distinct_key_not_created(temp_db, monkeypatch):
    row = _drive(temp_db, monkeypatch, _ENVELOPE)
    # Archived is the distinct envelope key (8), NOT memories_created (3).
    assert row["memories_archived"] == 8
    # Discovered still mirrors memories_created.
    assert row["memories_discovered"] == 3


def test_strengthened_passthrough_not_hardcoded_zero(temp_db, monkeypatch):
    row = _drive(temp_db, monkeypatch, _ENVELOPE)
    assert row["memories_strengthened"] == 4          # was always 0 before WS4.3
    assert row["memories_weakened"] == 5              # = memories_pruned


def test_missing_new_keys_default_zero(temp_db, monkeypatch):
    """An older agent that omits the new keys records 0, not a crash."""
    report = '```json\n{"metrics": {"memories_created": 2, "memories_pruned": 1}}\n```'
    row = _drive(temp_db, monkeypatch, report)
    assert row["memories_archived"] == 0
    assert row["memories_strengthened"] == 0
    assert row["memories_discovered"] == 2            # created still flows through
