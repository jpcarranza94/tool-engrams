"""Tests for the session_surfaces read/prune helpers added to session_state.py
(the bypasses that monitor / dashboard / recall / consolidate / mark-noise used
to write inline)."""

from __future__ import annotations

from toolengrams import memory_store
from toolengrams.retrieval import session_state as ss


def _mem(conn, name) -> int:
    return memory_store.insert_memory(
        conn, name=name, description=None, body="b", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=1000,
    )


def _surface(conn, session_id, memory_id, tool_use_id, ts):
    ss.log_surfaces(conn, session_id, [memory_id], tool_use_id,
                    ss.HOOK_PRE_TOOL_USE, turn_at_surface=1, now_ts=ts)


def test_count_surfaces_since(temp_db):
    a, b = _mem(temp_db, "a"), _mem(temp_db, "b")
    _surface(temp_db, "s1", a, "t1", ts=500)
    _surface(temp_db, "s1", b, "t2", ts=1500)
    assert ss.count_surfaces_since(temp_db, 1000) == 1   # only the ts=1500 one
    assert ss.count_surfaces_since(temp_db, 0) == 2


def test_recent_surfaces_with_memory_joins_name(temp_db):
    a = _mem(temp_db, "alpha")
    _surface(temp_db, "sess-xyz", a, "t1", ts=2000)
    rows = ss.recent_surfaces_with_memory(temp_db, limit=10)
    assert rows[0]["name"] == "alpha"
    assert rows[0]["memory_id"] == a
    assert rows[0]["session_id"] == "sess-xyz"


def test_surfaces_for_memory_scopes_and_orders(temp_db):
    a, b = _mem(temp_db, "a"), _mem(temp_db, "b")
    _surface(temp_db, "s", a, "t1", ts=100)
    _surface(temp_db, "s", b, "t2", ts=200)
    rows = ss.surfaces_for_memory(temp_db, a, limit=10)
    assert len(rows) == 1 and rows[0]["surfaced_ts"] == 100


def test_prune_surfaces_before(temp_db):
    a, b = _mem(temp_db, "a"), _mem(temp_db, "b")
    _surface(temp_db, "s", a, "t1", ts=100)
    _surface(temp_db, "s", b, "t2", ts=5000)
    assert ss.prune_surfaces_before(temp_db, 1000) == 1   # deleted the old one
    assert ss.count_surfaces_since(temp_db, 0) == 1


def test_mark_unmarked_noise_scoped_then_all(temp_db):
    a = _mem(temp_db, "a")
    _surface(temp_db, "s1", a, "t1", ts=100)
    _surface(temp_db, "s2", a, "t2", ts=200)
    assert ss.mark_unmarked_noise(temp_db, a, session_id="s1") == 1  # one session
    assert ss.mark_unmarked_noise(temp_db, a, session_id=None) == 1  # the rest
    assert ss.mark_unmarked_noise(temp_db, a, session_id=None) == 0  # nothing left
