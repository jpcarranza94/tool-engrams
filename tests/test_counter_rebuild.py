"""Counter integrity: useful_count/noise_count derive from session_surfaces.

Covers the q-input drift the consolidation run flagged — counters diverging
from the surface ground truth via the v12 zeroing, restore zeroing, and the old
per-call judge bump. See docs/adr/0013.
"""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import rebuild_counters


def _mem(conn, name="m", useful=0, noise=0):
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, "
        " created_ts, useful_count, noise_count) "
        "VALUES (?, '', 'b', 'hint', 'global', NULL, ?, ?, ?)",
        (name, int(time.time()), useful, noise),
    )
    return cur.lastrowid


def _surface(conn, memory_id, outcome, n=1):
    for i in range(n):
        conn.execute(
            "INSERT INTO session_surfaces (session_id, memory_id, surfaced_ts, hook, outcome) "
            "VALUES (?, ?, ?, 'PreToolUse', ?)",
            (f"sess-{memory_id}-{outcome}-{i}", memory_id, 1000 + i, outcome),
        )


def _counts(conn, mid):
    r = conn.execute("SELECT useful_count, noise_count FROM memories WHERE id=?", (mid,)).fetchone()
    return (r["useful_count"], r["noise_count"])


# ---------- recount_from_surfaces ----------


def test_recount_derives_counts_from_surfaces(temp_db):
    # Counter says 1/0 but surfaces say 8 helpful / 3 noise (the id-39 pattern).
    mid = _mem(temp_db, useful=1, noise=0)
    _surface(temp_db, mid, "helpful", n=8)
    _surface(temp_db, mid, "noise", n=3)
    _surface(temp_db, mid, "unused", n=5)  # unused must not count either way

    memory_store.recount_from_surfaces(temp_db, [mid])
    assert _counts(temp_db, mid) == (8, 3)


def test_recount_all_when_ids_none(temp_db):
    a = _mem(temp_db, "a", useful=99)
    b = _mem(temp_db, "b", noise=99)
    _surface(temp_db, a, "helpful", n=2)
    _surface(temp_db, b, "noise", n=1)

    memory_store.recount_from_surfaces(temp_db)  # all
    assert _counts(temp_db, a) == (2, 0)
    assert _counts(temp_db, b) == (0, 1)


def test_recount_empty_ids_is_noop(temp_db):
    assert memory_store.recount_from_surfaces(temp_db, []) == 0


# ---------- restore recomputes instead of zeroing ----------


def test_restore_recomputes_counters_not_zero(temp_db):
    # archive never touches counters; restore used to zero useful_count, losing
    # a proven memory's reputation. It must re-derive from surfaces instead.
    mid = _mem(temp_db)
    _surface(temp_db, mid, "helpful", n=6)
    _surface(temp_db, mid, "noise", n=1)
    memory_store.recount_from_surfaces(temp_db, [mid])
    assert _counts(temp_db, mid) == (6, 1)

    memory_store.archive(temp_db, mid)
    memory_store.restore(temp_db, mid)

    assert _counts(temp_db, mid) == (6, 1)  # NOT (0, 0)
    assert memory_store.get(temp_db, mid).archived_ts is None


# ---------- bump delta ----------


def test_bump_useful_delta(temp_db):
    mid = _mem(temp_db)
    memory_store.bump_useful(temp_db, [mid], delta=3)
    memory_store.bump_noise(temp_db, [mid], delta=2)
    assert _counts(temp_db, mid) == (3, 2)
    memory_store.bump_useful(temp_db, [mid], delta=0)  # no-op
    assert _counts(temp_db, mid) == (3, 2)


# ---------- CLI ----------


def test_rebuild_counters_cli_applies_and_is_idempotent(temp_db, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAM_DB", "")  # force the --db path below
    mid = _mem(temp_db, useful=0, noise=0)
    _surface(temp_db, mid, "helpful", n=4)
    _surface(temp_db, mid, "noise", n=1)
    temp_db.commit()
    db_path = temp_db.execute("PRAGMA database_list").fetchone()["file"]

    rc = rebuild_counters.main(["--db", db_path])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["drifted_memories"] == 1
    assert out["changes"][0]["useful_count"] == [0, 4]

    # Re-open to see the committed write, then a second run finds no drift.
    assert rebuild_counters.main(["--db", db_path]) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["drifted_memories"] == 0


def test_rebuild_counters_dry_run_does_not_write(temp_db, capsys):
    mid = _mem(temp_db, useful=0)
    _surface(temp_db, mid, "helpful", n=2)
    temp_db.commit()
    db_path = temp_db.execute("PRAGMA database_list").fetchone()["file"]

    assert rebuild_counters.main(["--db", db_path, "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["drifted_memories"] == 1
    assert _counts(temp_db, mid) == (0, 0)  # unchanged on disk
