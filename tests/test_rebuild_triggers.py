"""Unit tests for `engram rebuild-triggers`."""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import rebuild_triggers


def _seed_memory_no_triggers(conn, name: str, body: str, kind: str = "hint") -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, ?, ?, 'global', NULL, ?)",
        (name, body, kind, now_ts),
    )
    return cur.lastrowid


def test_rebuild_extracts_from_backticks(temp_db, monkeypatch, capsys):
    mid = _seed_memory_no_triggers(
        temp_db, "rule",
        "Use `git push --force-with-lease` instead of `--force`. See ~/.gitconfig.",
    )
    monkeypatch.setenv("ENGRAM_DB", temp_db.execute("PRAGMA database_list").fetchone()[2])

    rc = rebuild_triggers.main([])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rebuilt"] >= 1

    rows = temp_db.execute(
        "SELECT kind, tokens_json, path_pattern FROM triggers WHERE memory_id = ?",
        (mid,),
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert "token_subseq" in kinds
    # Either token_subseq (git push) or path_glob (~/.gitconfig) should appear.
    assert len(rows) >= 1


def test_rebuild_skips_memories_without_extractable_patterns(temp_db, capsys):
    # Body with no backticks, no paths, no URLs — nothing extractable.
    _seed_memory_no_triggers(temp_db, "bare", "Just a sentence with no markers.")
    rc = rebuild_triggers.main([])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["no_triggers_extracted"] >= 1
    assert summary["rebuilt"] == 0


def test_dry_run_does_not_write(temp_db, capsys):
    mid = _seed_memory_no_triggers(
        temp_db, "dry", "Use `mycli foo` for the thing.",
    )
    rc = rebuild_triggers.main(["--dry-run"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "dry_run"
    assert summary["rebuilt"] >= 1

    # No triggers actually inserted.
    row = temp_db.execute(
        "SELECT COUNT(*) AS n FROM triggers WHERE memory_id = ?", (mid,),
    ).fetchone()
    assert row["n"] == 0


def test_rebuild_replaces_existing_triggers_in_default_mode(temp_db, capsys):
    mid = _seed_memory_no_triggers(temp_db, "rule", "Use `git status` to check.")
    # Seed a stale trigger.
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', 'bogus', '[\"bogus\"]')",
        (mid,),
    )

    rebuild_triggers.main([])

    rows = temp_db.execute(
        "SELECT tokens_json FROM triggers WHERE memory_id = ?", (mid,),
    ).fetchall()
    token_sets = [json.loads(r["tokens_json"]) for r in rows]
    assert ["bogus"] not in token_sets
    assert any("git" in t for t in token_sets)


def test_only_triggerless_skips_memories_that_already_have_triggers(temp_db, capsys):
    mid = _seed_memory_no_triggers(temp_db, "rule", "Use `git status` to check.")
    # Give it a trigger.
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', 'git', '[\"git\", \"status\"]')",
        (mid,),
    )

    rebuild_triggers.main(["--only-triggerless"])
    summary = json.loads(capsys.readouterr().out)
    # Should not have rebuilt this one.
    assert summary["total_memories_considered"] == 0


def test_rebuild_preserves_tuned_access_mode(temp_db, monkeypatch, capsys):
    """A path trigger tuned via `engram trigger --access-mode` keeps its mode
    across a rebuild, even though the mode isn't expressible in body text."""
    mid = _seed_memory_no_triggers(temp_db, "rule", "Edit ~/.gitconfig with care.")
    # Tune the path trigger whose pattern the body extractor will re-derive.
    memory_store.add_path_trigger(temp_db, mid, "~/.gitconfig", access_mode="any")
    monkeypatch.setenv("ENGRAM_DB", temp_db.execute("PRAGMA database_list").fetchone()[2])

    rebuild_triggers.main([])

    rows = temp_db.execute(
        "SELECT path_pattern, access_mode FROM triggers "
        "WHERE memory_id=? AND kind='path_glob'",
        (mid,),
    ).fetchall()
    modes = {r["path_pattern"]: r["access_mode"] for r in rows}
    assert modes["~/.gitconfig"] == "any"          # tuned mode preserved
    # A pattern with no prior tuning falls back to the default.
    assert modes.get("**/.gitconfig") == "write"


def test_archived_memories_skipped(temp_db, capsys):
    mid = _seed_memory_no_triggers(temp_db, "archived", "Use `git push` again.")
    temp_db.execute("UPDATE memories SET archived_ts = ? WHERE id = ?", (int(time.time()), mid))

    rebuild_triggers.main([])
    summary = json.loads(capsys.readouterr().out)
    assert summary["total_memories_considered"] == 0
