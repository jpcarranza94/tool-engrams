"""engram seed — first-run smoke-test memories.

Pins the first-run safety contract: the default set must never contain a
block-kind memory (a seeded block denies real tool calls), --with-block is
the explicit opt-in, and --remove deletes exactly the seed names without
fuzzy-matching into user memories.
"""

from __future__ import annotations

import time

from toolengrams import memory_store
from toolengrams.cli import seed
from toolengrams.retrieval.session_state import (
    HOOK_PRE_TOOL_USE,
    has_pending_surfaces,
    log_surfaces,
)


def test_default_seed_is_hint_only(temp_db, capsys):
    assert seed.main([]) == 0
    kinds = {m["kind"] for m in seed.SEED_MEMORIES}
    assert kinds == {"hint"}
    rows = temp_db.execute(
        "SELECT kind FROM memories WHERE archived_ts IS NULL"
    ).fetchall()
    assert len(rows) == len(seed.SEED_MEMORIES)
    assert {r["kind"] for r in rows} == {"hint"}


def test_with_block_adds_the_block_demo(temp_db, capsys):
    assert seed.main(["--with-block"]) == 0
    rows = temp_db.execute("SELECT name, kind FROM memories").fetchall()
    blocks = [r for r in rows if r["kind"] == "block"]
    assert len(blocks) == len(seed.BLOCK_SEED_MEMORIES)
    assert len(rows) == len(seed.SEED_MEMORIES) + len(seed.BLOCK_SEED_MEMORIES)


def test_seed_is_idempotent(temp_db, capsys):
    seed.main([])
    seed.main([])
    count = temp_db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == len(seed.SEED_MEMORIES)
    out = capsys.readouterr().out
    assert "already present" in out


def test_remove_deletes_only_seed_memories(temp_db, capsys):
    seed.main(["--with-block"])
    memory_store.insert_memory(
        temp_db,
        name="my own memory about ssh",
        description="user memory",
        body="ssh somewhere",
        kind="hint",
        scope="global",
        project_slug=None,
        pinned=False,
        created_ts=int(time.time()),
    )

    assert seed.main(["--remove"]) == 0
    rows = temp_db.execute("SELECT name FROM memories").fetchall()
    names = {r["name"] for r in rows}
    assert names == {"my own memory about ssh"}


def test_remove_does_not_fuzzy_match(temp_db):
    # No seeds present; a user memory that find_by_name would fuzzy-hit
    # from a seed name must survive --remove.
    memory_store.insert_memory(
        temp_db,
        name="replica notes",
        description="mentions psql replica read-only behaviour",
        body="the psql replica is read-only most days",
        kind="hint",
        scope="global",
        project_slug=None,
        pinned=False,
        created_ts=int(time.time()),
    )
    assert seed.main(["--remove"]) == 0
    count = temp_db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 1


def test_seed_realigns_legacy_block_kind(temp_db, capsys):
    """A DB seeded under the old version holds the git-commit memory as a
    block (denying every commit). Re-running seed must downgrade it to the
    shipped hint kind instead of skipping it."""
    legacy = seed.SEED_MEMORIES[1]
    assert "git commit" in legacy["name"]
    memory_store.insert_memory(
        temp_db,
        name=legacy["name"],
        description=legacy["description"],
        body=legacy["body"],
        kind="block",
        scope="global",
        project_slug=None,
        pinned=False,
        created_ts=int(time.time()),
    )

    assert seed.main([]) == 0
    row = temp_db.execute(
        "SELECT kind FROM memories WHERE name = ?", (legacy["name"],)
    ).fetchone()
    assert row["kind"] == "hint"
    assert "fixed   [block→hint]" in capsys.readouterr().out


def test_remove_clears_pending_surfaces(temp_db, capsys):
    """Hard-deleting a surfaced seed must not leave outcome=NULL surface rows
    behind — they keep has_pending_surfaces() true forever."""
    seed.main([])
    mem = memory_store.find_by_name(temp_db, seed.SEED_MEMORIES[0]["name"])
    log_surfaces(temp_db, "sess-rm", [mem.id], "tu-1",
                 HOOK_PRE_TOOL_USE, 1, int(time.time()))
    assert has_pending_surfaces(temp_db, "sess-rm")

    assert seed.main(["--remove"]) == 0
    assert not has_pending_surfaces(temp_db, "sess-rm")
    count = temp_db.execute("SELECT COUNT(*) FROM session_surfaces").fetchone()[0]
    assert count == 0


def test_seed_output_lists_kind_and_trigger(temp_db, capsys):
    seed.main([])
    out = capsys.readouterr().out
    assert "[hint] psql replica is read-only" in out
    assert "psql -h" in out
    assert "engram seed --remove" in out
