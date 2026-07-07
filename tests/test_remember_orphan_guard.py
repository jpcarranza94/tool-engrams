"""Regression: the formation specificity gate must never orphan or wipe triggers.

`insert_candidate_triggers` silently DROPS too-broad triggers (bare subcommand
tools, extension-only / common-basename globs). Without a pre-insert check, two
data-loss bugs follow:

  1. NEW memory whose ONLY trigger is broad → inserted with ZERO triggers (a
     permanent orphan that can never surface), falsely reported as `inserted`.
  2. MERGE/fold (`remember --into <id>`) whose supplied triggers are all broad →
     `update_existing_memory` deletes the target's good triggers then inserts
     nothing, WIPING a previously-surfacing memory.

`is_persistable_trigger` (mirrored into `_resolve_triggers` and
`update_existing_memory`) closes both. These are through-CLI tests so they cover
the real `engram remember` path, not just the predicate.
"""

from __future__ import annotations

import io
import json
import time

from toolengrams import memory_store
from toolengrams.cli import remember, trigger


def _run(argv, monkeypatch, capsys):
    """Run remember.main, return (rc, parsed_stdout_or_None)."""
    rc = remember.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, (json.loads(out) if out else None)


def _count_memories(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]


def _triggers(conn, mid):
    return conn.execute(
        "SELECT kind, tokens_json, path_pattern FROM triggers WHERE memory_id = ?",
        (mid,),
    ).fetchall()


# ---------- NEW memory: all-broad trigger must not orphan ----------


def test_broad_path_glob_only_is_refused_not_orphaned(temp_db, monkeypatch, capsys):
    # The --path help-text example itself: an extension-only glob is too broad.
    rc, out = _run(["--path", "**/*.py", "body about python files"], monkeypatch, capsys)
    assert rc == 1
    assert out["error"] == "no_triggers"
    assert _count_memories(temp_db) == 0  # nothing inserted — no orphan


def test_bare_subcommand_trigger_only_is_refused_not_orphaned(temp_db, monkeypatch, capsys):
    rc, out = _run(["--trigger", "git", "body about git"], monkeypatch, capsys)
    assert rc == 1
    assert out["error"] == "no_triggers"
    assert _count_memories(temp_db) == 0


def test_bare_subcommand_from_body_backtick_is_refused(temp_db, monkeypatch, capsys):
    # Body whose only backticked command reduces to a single subcommand-tool token.
    rc, out = _run(["Run `psql` against the replica"], monkeypatch, capsys)
    assert rc == 1
    assert out["error"] == "no_triggers"
    assert _count_memories(temp_db) == 0


# ---------- mixed: keep the specific, drop the broad ----------


def test_mixed_triggers_keeps_only_the_specific_one(temp_db, monkeypatch, capsys):
    rc, out = _run(
        ["--trigger", "git", "--trigger", "git push --force", "body text"],
        monkeypatch, capsys,
    )
    assert rc == 0
    assert out["action"] == "inserted"
    mid = out["memory"]["id"]
    rows = _triggers(temp_db, mid)
    token_sets = [json.loads(r["tokens_json"]) for r in rows if r["kind"] == "token_subseq"]
    assert token_sets == [["git", "push", "--force"]]  # bare "git" dropped


# ---------- block/pinned may bind broad safety triggers (exempt_broad) ----------


def test_block_with_only_broad_glob_is_persisted(temp_db, monkeypatch, capsys):
    # A safety block legitimately needs a broad trigger; exempt_broad waives the
    # specificity refusal for kind=block, so this is NOT treated as no_triggers.
    rc, out = _run(
        ["--kind", "block", "--path", "**/*.pem", "Never read private keys"],
        monkeypatch, capsys,
    )
    assert rc == 0
    assert out["action"] == "inserted"
    mid = out["memory"]["id"]
    globs = [r["path_pattern"] for r in _triggers(temp_db, mid) if r["kind"] == "path_glob"]
    assert globs == ["**/*.pem"]


def test_pinned_with_only_bare_subcommand_token_is_persisted(temp_db, monkeypatch, capsys):
    rc, out = _run(
        ["--pinned", "--trigger", "git", "Always sanity-check the branch first"],
        monkeypatch, capsys,
    )
    assert rc == 0
    assert out["action"] == "inserted"
    mid = out["memory"]["id"]
    token_sets = [json.loads(r["tokens_json"]) for r in _triggers(temp_db, mid)
                  if r["kind"] == "token_subseq"]
    assert token_sets == [["git"]]


def test_hint_with_only_broad_glob_is_still_refused(temp_db, monkeypatch, capsys):
    # Contrast: a hint (the default kind) gets no exemption — broad → no_triggers.
    rc, out = _run(["--kind", "hint", "--path", "**/*.pem", "note"], monkeypatch, capsys)
    assert rc == 1
    assert out["error"] == "no_triggers"
    assert _count_memories(temp_db) == 0


def test_trigger_cli_adds_broad_trigger_to_block(temp_db, monkeypatch, capsys):
    # The `engram trigger` lever also honors the block/pinned broad exemption.
    mid = memory_store.insert_memory(
        temp_db, name="pem-block", description="", body="never read keys",
        kind="block", scope="global", project_slug=None, pinned=False,
        created_ts=int(time.time()),
    )
    memory_store.add_token_trigger(temp_db, mid, ["cat", "id_rsa"])
    temp_db.commit()

    rc = trigger.main([str(mid), "--add-path", "**/*.pem"])
    assert rc == 0
    globs = [r["path_pattern"] for r in _triggers(temp_db, mid) if r["kind"] == "path_glob"]
    assert "**/*.pem" in globs


# ---------- MERGE/fold: an all-broad body must not wipe the target ----------


def _seed_target(conn) -> int:
    mid = memory_store.insert_memory(
        conn, name="deploy-note", description="", body="original body",
        kind="hint", scope="global", project_slug=None, pinned=False,
        created_ts=int(time.time()),
    )
    memory_store.add_token_trigger(conn, mid, ["git", "push"])
    conn.commit()
    return mid


def test_into_with_all_broad_body_keeps_target_triggers(temp_db, monkeypatch, capsys):
    mid = _seed_target(temp_db)
    before = _triggers(temp_db, mid)
    assert len(before) == 1

    # Merge a body whose only trigger candidate (`git`) is too broad to persist.
    rc, out = _run(
        ["--into", str(mid), "Updated guidance mentioning `git` bare"],
        monkeypatch, capsys,
    )
    assert rc == 0
    assert out["action"] == "merged_into"

    after = _triggers(temp_db, mid)
    assert len(after) == 1  # NOT wiped
    assert json.loads(after[0]["tokens_json"]) == ["git", "push"]
    # Body was still updated (the merge itself succeeded).
    body = temp_db.execute("SELECT body FROM memories WHERE id = ?", (mid,)).fetchone()["body"]
    assert "Updated guidance" in body


def test_into_with_only_broad_extra_trigger_keeps_target_triggers(temp_db, monkeypatch, capsys):
    # Exercises the dedup.py wipe-gate hardening specifically for the
    # --extra-trigger dict path (not filtered by _resolve_triggers).
    mid = _seed_target(temp_db)
    rc, out = _run(
        ["--into", str(mid), "--extra-trigger", "path_glob:**/*.py",
         "Body with no bindable inline content"],
        monkeypatch, capsys,
    )
    assert rc == 0
    after = _triggers(temp_db, mid)
    assert len(after) == 1  # broad extra dropped → no wipe
    assert json.loads(after[0]["tokens_json"]) == ["git", "push"]
