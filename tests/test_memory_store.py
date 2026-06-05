"""Tests for the Memory aggregate persistence seam (memory_store.py)."""

from __future__ import annotations

import time

from toolengrams import memory_store as ms
from toolengrams.models import Memory


def _insert(conn, **over) -> int:
    fields = dict(
        name="m", description="d", body="body text", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    fields.update(over)
    return ms.insert_memory(conn, **fields)


# ---------- insert / get roundtrip ----------


def test_insert_and_get_roundtrip(temp_db):
    mid = _insert(temp_db, name="alpha", body="hello world", kind="block")
    mem = ms.get(temp_db, mid)
    assert isinstance(mem, Memory)
    assert mem.id == mid
    assert mem.name == "alpha"
    assert mem.body == "hello world"
    assert mem.kind == "block"
    assert mem.pinned is False
    assert mem.archived_ts is None


def test_get_missing_returns_none(temp_db):
    assert ms.get(temp_db, 999) is None


# ---------- find_by_name (exact → fts → like) ----------


def test_find_by_name_exact(temp_db):
    mid = _insert(temp_db, name="git push force")
    assert ms.find_by_name(temp_db, "git push force").id == mid


def test_find_by_name_fuzzy_fts(temp_db):
    mid = _insert(temp_db, name="force push lease", body="use force-with-lease")
    # Not an exact name; FTS should still resolve it.
    found = ms.find_by_name(temp_db, "force lease")
    assert found is not None and found.id == mid


def test_find_by_name_excludes_archived_by_default(temp_db):
    mid = _insert(temp_db, name="archived one")
    ms.archive(temp_db, mid)
    assert ms.find_by_name(temp_db, "archived one") is None
    assert ms.find_by_name(temp_db, "archived one", include_archived=True).id == mid


def test_name_exists(temp_db):
    _insert(temp_db, name="exists")
    assert ms.name_exists(temp_db, "exists") is True
    assert ms.name_exists(temp_db, "nope") is False


# ---------- list / search / order ----------


def test_list_memories_excludes_archived(temp_db):
    a = _insert(temp_db, name="active")
    arch = _insert(temp_db, name="arch")
    ms.archive(temp_db, arch)
    ids = {m.id for m in ms.list_memories(temp_db)}
    assert a in ids and arch not in ids
    assert arch in {m.id for m in ms.list_memories(temp_db, include_archived=True)}


def test_list_memories_audit_order_never_verified_first(temp_db):
    verified = _insert(temp_db, name="verified")
    ms.set_verified(temp_db, verified, int(time.time()))
    never = _insert(temp_db, name="never")
    order = [m.id for m in ms.list_memories(temp_db, order="audit")]
    assert order.index(never) < order.index(verified)


def test_search_matches_body(temp_db):
    mid = _insert(temp_db, name="docker note", body="use docker buildx not build")
    hits = ms.search(temp_db, "buildx")
    assert mid in {m.id for m in hits}


# ---------- triggers + hot-path match ----------


def test_triggers_roundtrip_and_match(temp_db):
    mid = _insert(temp_db, name="gh pr", scope="global")
    ms.add_token_trigger(temp_db, mid, "gh", ["gh", "pr", "create"])
    ms.add_path_trigger(temp_db, mid, "*.tf")

    trigs = ms.triggers_for(temp_db, mid)
    kinds = {t.kind for t in trigs}
    assert kinds == {"token_subseq", "path_glob"}
    tok = [t for t in trigs if t.kind == "token_subseq"][0]
    assert tok.tokens == ["gh", "pr", "create"]

    rows = ms.match_token_triggers(temp_db, "gh", project_slug=None, kind=None)
    assert mid in {r["id"] for r in rows}
    prows = ms.match_path_triggers(temp_db, project_slug=None, kind=None)
    assert mid in {r["id"] for r in prows}


def test_delete_triggers_and_single(temp_db):
    mid = _insert(temp_db)
    ms.add_token_trigger(temp_db, mid, "a", ["a", "b"])
    ms.add_token_trigger(temp_db, mid, "c", ["c", "d"])
    assert ms.count_triggers_for(temp_db, mid) == 2
    one = ms.triggers_for(temp_db, mid)[0]
    ms.delete_trigger(temp_db, one.id)
    assert ms.count_triggers_for(temp_db, mid) == 1
    ms.delete_triggers_for(temp_db, mid)
    assert ms.count_triggers_for(temp_db, mid) == 0


def test_count_trigger_owners(temp_db):
    a = _insert(temp_db, name="a")
    b = _insert(temp_db, name="b")
    ms.add_token_trigger(temp_db, a, "git", ["git", "push"])
    ms.add_token_trigger(temp_db, b, "git", ["git", "push"])
    assert ms.count_token_trigger_owners(temp_db, ["git", "push"]) == 2
    assert ms.count_token_trigger_owners(temp_db, ["git", "pull"]) == 0


# ---------- counter bumps ----------


def test_bump_surface_and_useful(temp_db):
    mid = _insert(temp_db)
    now = int(time.time())
    ms.bump_surface(temp_db, [mid], now)
    ms.bump_surface(temp_db, [mid], now)
    ms.bump_useful(temp_db, [mid])
    mem = ms.get(temp_db, mid)
    assert mem.surface_count == 2 and mem.useful_count == 1
    assert mem.last_surfaced_ts == now


def test_soft_demote_and_restore(temp_db):
    mid = _insert(temp_db)
    ms.bump_surface(temp_db, [mid], int(time.time()))
    ms.bump_useful(temp_db, [mid])
    ms.soft_demote(temp_db, mid)
    mem = ms.get(temp_db, mid)
    assert mem.useful_count == 0 and mem.surface_count == 1 + ms.SOFT_DEMOTE_PENALTY
    ms.restore(temp_db, mid)
    mem = ms.get(temp_db, mid)
    assert mem.surface_count == 0 and mem.useful_count == 0 and mem.archived_ts is None


def test_archive_restore(temp_db):
    mid = _insert(temp_db)
    ms.archive(temp_db, mid, 12345)
    assert ms.get(temp_db, mid).archived_ts == 12345
    ms.restore(temp_db, mid)
    assert ms.get(temp_db, mid).archived_ts is None


# ---------- update / delete / pin / verify ----------


def test_update_memory(temp_db):
    mid = _insert(temp_db, name="old", body="old body", kind="hint")
    ms.update_memory(temp_db, mid, name="new", description="x", body="new body",
                     kind="block", pinned=True, created_ts=999)
    mem = ms.get(temp_db, mid)
    assert mem.name == "new" and mem.body == "new body" and mem.kind == "block"
    assert mem.pinned is True


def test_set_pinned_and_verified(temp_db):
    mid = _insert(temp_db)
    ms.set_pinned(temp_db, mid, True)
    assert ms.get(temp_db, mid).pinned is True
    ms.set_verified(temp_db, mid, 555)
    assert ms.get(temp_db, mid).last_verified_ts == 555


def test_delete_memory_cascades_triggers(temp_db):
    mid = _insert(temp_db)
    ms.add_token_trigger(temp_db, mid, "x", ["x"])
    ms.delete_memory(temp_db, mid)
    assert ms.get(temp_db, mid) is None
    assert ms.count_triggers_for(temp_db, mid) == 0


# ---------- aggregates ----------


def test_summary_and_health_stats(temp_db):
    h = _insert(temp_db, name="h", kind="hint")
    b = _insert(temp_db, name="b", kind="block")
    ms.add_token_trigger(temp_db, h, "git", ["git"])
    ms.bump_surface(temp_db, [h], int(time.time()))
    ms.bump_useful(temp_db, [h])

    s = ms.summary_stats(temp_db)
    assert s["by_kind"]["hint"] == 1 and s["by_kind"]["block"] == 1
    assert s["triggers_by_kind"]["token_subseq"] == 1

    health = ms.health_stats(temp_db)
    assert health["active"] == 2
    assert health["total_surfaces"] == 1 and health["total_useful"] == 1
