"""path_glob access-mode filtering (issue #63).

A path trigger carries a read-vs-write intent; the call carries one derived
from its tool name. An edit-intended (`write`) memory must stop firing on mere
reads, while `any` keeps the pre-#63 fire-on-everything behavior and legacy
NULL rows fail open (match any call).

Two layers exercised:
  - memory_store.match_path_triggers: the cheap WHERE-clause filter.
  - retrieve_candidates: the full extract → derive-mode → match chain.
"""

from __future__ import annotations

import time

from toolengrams import memory_store as ms
from toolengrams.models import ExtractedTriggerHint
from toolengrams.retrieval.rank import retrieve_candidates


def _path_mem(conn, access_mode: str, *, pattern: str = "**/*.py") -> int:
    mid = ms.insert_memory(
        conn, name=f"m-{access_mode}", description="", body="body", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    ms.add_path_trigger(conn, mid, pattern, access_mode=access_mode)
    return mid


def _ids(conn, call_mode: str) -> set[int]:
    return {r["id"] for r in ms.match_path_triggers(conn, None, None, call_mode)}


# ---------- match_path_triggers filter ----------


def test_write_trigger_matches_write_call_not_read(temp_db):
    mid = _path_mem(temp_db, "write")
    assert mid in _ids(temp_db, "write")
    assert mid not in _ids(temp_db, "read")


def test_read_trigger_matches_read_call_not_write(temp_db):
    mid = _path_mem(temp_db, "read")
    assert mid in _ids(temp_db, "read")
    assert mid not in _ids(temp_db, "write")


def test_any_trigger_matches_both_modes(temp_db):
    mid = _path_mem(temp_db, "any")
    assert mid in _ids(temp_db, "write")
    assert mid in _ids(temp_db, "read")


def test_any_call_mode_skips_filter(temp_db):
    """A Bash call (ACCESS_ANY) can read or write, so it matches every path
    trigger regardless of the trigger's own mode."""
    write_id = _path_mem(temp_db, "write")
    read_id = _path_mem(temp_db, "read")
    matched = _ids(temp_db, "any")
    assert {write_id, read_id} <= matched


def test_block_path_memory_exempt_from_access_filter(temp_db):
    """A block is enforcement, not surfacing: a write-mode path block must still
    fire on a read call (it must never go silent because the backfill set it to
    'write'). Mirrors the q-gate / same-session block exemptions."""
    mid = ms.insert_memory(
        temp_db, name="blk", description="", body="body", kind="block",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    ms.add_path_trigger(temp_db, mid, "**/*.py", access_mode="write")
    assert mid in _ids(temp_db, "read")   # would be filtered out if it were a hint
    assert mid in _ids(temp_db, "write")


def test_legacy_null_access_mode_fails_open(temp_db):
    """A path trigger written before v17 (access_mode NULL) keeps matching any
    call — the migration backfills real rows, NULL is the defensive fallback."""
    mid = ms.insert_memory(
        temp_db, name="legacy", description="", body="body", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) "
        "VALUES (?, 'path_glob', '**/*.py')", (mid,))
    assert mid in _ids(temp_db, "read")
    assert mid in _ids(temp_db, "write")


# ---------- retrieve_candidates (full chain) ----------


def _hint(tool_name: str, path: str) -> ExtractedTriggerHint:
    return ExtractedTriggerHint(tool_name=tool_name, paths=[path])


def test_retrieve_write_trigger_fires_on_edit_not_read(temp_db):
    mid = _path_mem(temp_db, "write")
    edit = retrieve_candidates(temp_db, _hint("Edit", "/proj/main.py"), None)
    read = retrieve_candidates(temp_db, _hint("Read", "/proj/main.py"), None)
    assert mid in {c.memory_id for c in edit}
    assert mid not in {c.memory_id for c in read}


def test_retrieve_hint_kind_respects_access_mode(temp_db):
    """The failure-surface hook calls retrieve_candidates(kind='hint'); the
    access filter must apply on that path too — a write hint stays suppressed on
    a read-only failed call, fires on a write one."""
    mid = _path_mem(temp_db, "write")  # _path_mem creates kind='hint'
    read = retrieve_candidates(temp_db, _hint("Read", "/proj/main.py"), None, kind="hint")
    edit = retrieve_candidates(temp_db, _hint("Edit", "/proj/main.py"), None, kind="hint")
    assert mid not in {c.memory_id for c in read}
    assert mid in {c.memory_id for c in edit}


def test_retrieve_default_path_trigger_is_write(temp_db):
    """add_path_trigger defaults to 'write', so a body-formed path memory no
    longer surfaces on a Read of the same file."""
    mid = _path_mem(temp_db, "write")  # mirrors formation default
    read = retrieve_candidates(temp_db, _hint("Read", "/proj/main.py"), None)
    grep = retrieve_candidates(temp_db, _hint("Grep", "/proj/main.py"), None)
    assert mid not in {c.memory_id for c in read}
    assert mid not in {c.memory_id for c in grep}
