"""Unit tests for the `engram remember` CLI handler."""

from __future__ import annotations

import io
import json

import pytest

from toolengrams.commands import remember


def _run(argv: list[str], monkeypatch, stdin: str | None = None, capsys=None) -> dict:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = remember.main(argv)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def _rows(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


# ---------- body + name ----------


def test_positional_text_inserts_memory(temp_db, monkeypatch, capsys):
    payload = _run(["some body about `git status`"], monkeypatch, capsys=capsys)
    assert payload["action"] == "inserted"
    assert payload["memory"]["id"] is not None
    rows = _rows(temp_db, "SELECT name, body, type, scope FROM memories")
    assert len(rows) == 1
    assert "git status" in rows[0]["body"]


def test_stdin_body_when_text_is_dash(temp_db, monkeypatch, capsys):
    payload = _run(["-"], monkeypatch, stdin="body via stdin `mycli`\n", capsys=capsys)
    assert payload["action"] == "inserted"
    rows = _rows(temp_db, "SELECT body FROM memories")
    assert rows[0]["body"].startswith("body via stdin")


def test_empty_body_returns_exit_2(temp_db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    # Simulate non-tty stdin but empty
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = remember.main([])
    assert rc == 2


def test_name_synthesized_from_first_line(temp_db, monkeypatch, capsys):
    body = "First line is the synthesized name\nSecond line has more context `git`."
    payload = _run([body], monkeypatch, capsys=capsys)
    assert payload["memory"]["name"] == "First line is the synthesized name"


def test_name_override_respected(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--name", "custom name", "body with `git`"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["name"] == "custom name"


def test_long_first_line_is_truncated(temp_db, monkeypatch, capsys):
    long = "x" * 200 + " use `git push`"
    payload = _run([long], monkeypatch, capsys=capsys)
    assert len(payload["memory"]["name"]) == 80


# ---------- triggers ----------


def test_extraction_emits_expected_triggers(temp_db, monkeypatch, capsys):
    body = "Use `git push` and see ~/.claude/settings.json, docs at https://example.com"
    payload = _run([body], monkeypatch, capsys=capsys)

    heads = {
        (t["tool_name"], tuple(t["head"]))
        for t in payload["extracted_triggers"]
        if t["kind"] == "tool_head"
    }
    assert ("Bash", ("git", "push")) in heads
    assert ("Bash", ("git",)) not in heads  # head-1 suppressed when head-2 exists
    assert ("WebFetch", ("example.com",)) in heads

    globs = {
        t["path_pattern"]
        for t in payload["extracted_triggers"]
        if t["kind"] == "path_glob"
    }
    assert "~/.claude/settings.json" in globs


def test_triggers_are_persisted_to_db(temp_db, monkeypatch, capsys):
    _run(["`git push`"], monkeypatch, capsys=capsys)
    rows = _rows(
        temp_db,
        "SELECT kind, tool_name, head_joined, head_length FROM triggers ORDER BY id",
    )
    kinds = [(r["kind"], r["tool_name"], r["head_joined"], r["head_length"]) for r in rows]
    assert ("tool_head", "Bash", "git push", 2) in kinds
    assert ("tool_head", "Bash", "git", 1) not in kinds  # suppressed


# ---------- extra triggers ----------


def test_extra_trigger_keyword_now_rejected(temp_db, monkeypatch):
    """keyword triggers were removed in the tool-bound refactor."""
    with pytest.raises(SystemExit):
        remember.main(["--extra-trigger", "keyword:mycli", "body text"])


def test_extra_trigger_tool_head(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--extra-trigger", "tool_head:Bash:git,push", "some `git` body"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["extra_triggers"][0]["kind"] == "tool_head"
    # JSON round-trips tuples to lists.
    assert list(payload["extra_triggers"][0]["head"]) == ["git", "push"]


def test_extra_trigger_malformed_raises(temp_db, monkeypatch):
    with pytest.raises(SystemExit):
        remember.main(["--extra-trigger", "nonsense", "body"])


# ---------- dry run ----------


def test_dry_run_does_not_insert(temp_db, monkeypatch, capsys):
    payload = _run(["--dry-run", "`git push`"], monkeypatch, capsys=capsys)
    assert payload["action"] == "dry_run"
    assert payload["memory"]["id"] is None
    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories")
    assert rows[0]["c"] == 0


# ---------- scope / type ----------


def test_scope_global_stores_null_project_slug(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--scope", "global", "body with `git`"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["project_slug"] is None
    rows = _rows(temp_db, "SELECT scope, project_slug FROM memories")
    assert rows[0]["scope"] == "global"
    assert rows[0]["project_slug"] is None


def test_scope_project_defaults_slug_from_cwd(temp_db, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAM_PROJECT_CWD", "/tmp/fake/project")
    payload = _run(["use `make build` here"], monkeypatch, capsys=capsys)
    assert payload["memory"]["project_slug"] == "-tmp-fake-project"


def test_scope_project_with_override(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--scope", "project", "--project-slug", "custom-slug", "use `make test`"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["project_slug"] == "custom-slug"


def test_invalid_type_returns_2(temp_db, monkeypatch, capsys):
    rc = remember.main(["--type", "bogus", "body"])
    assert rc == 2


def test_pinned_flag_stored(temp_db, monkeypatch, capsys):
    _run(["--pinned", "use `make deploy` carefully"], monkeypatch, capsys=capsys)
    rows = _rows(temp_db, "SELECT pinned FROM memories")
    assert rows[0]["pinned"] == 1


# ---------- dedup ----------


def test_dedup_updates_existing_on_trigger_overlap(temp_db, monkeypatch, capsys):
    """Second memory with same triggers should UPDATE, not INSERT."""
    p1 = _run(["`git push` -- always force push"], monkeypatch, capsys=capsys)
    assert p1["action"] == "inserted"
    mid = p1["memory"]["id"]

    p2 = _run(["`git push` -- never force push actually"], monkeypatch, capsys=capsys)
    assert p2["action"] == "updated"
    assert p2["memory"]["id"] == mid
    assert p2["existing_match"]["overlap_count"] >= 1  # (git, push) shared + name match

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 1  # only one memory, not two

    body = _rows(temp_db, "SELECT body FROM memories WHERE id = ?", mid)
    assert "never force push" in body[0]["body"]  # body was replaced


def test_dedup_allows_distinct_memories(temp_db, monkeypatch, capsys):
    """Memories with different triggers should both insert."""
    _run(["`git push` rule"], monkeypatch, capsys=capsys)
    p2 = _run(["`docker compose up` rule"], monkeypatch, capsys=capsys)
    assert p2["action"] == "inserted"

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 2


def test_dedup_same_trigger_different_body_updates(temp_db, monkeypatch, capsys):
    """Same trigger (git push) with different body → should update."""
    _run(["--name", "git push rule", "`git push` -- always to origin"], monkeypatch, capsys=capsys)
    p2 = _run(["--name", "git push updated", "`git push` -- with lease"], monkeypatch, capsys=capsys)
    # (git, push) overlaps → update
    assert p2["action"] == "updated"

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 1


# ---------- triggerless rejection ----------


def test_triggerless_body_rejected(temp_db, monkeypatch, capsys):
    """Body with no backticked commands or paths should be rejected."""
    rc = remember.main(["The staging DB is on port 5433."])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error"] == "no_triggers"


def test_body_with_only_paths_is_accepted(temp_db, monkeypatch, capsys):
    """Paths are valid triggers even without backticked commands."""
    payload = _run(["Config lives at ~/.claude/settings.json"], monkeypatch, capsys=capsys)
    assert payload["action"] == "inserted"


# ---------- vocabulary consolidation ----------


def test_consolidation_counts_on_update(temp_db, monkeypatch, capsys):
    _run(["`git push` one"], monkeypatch, capsys=capsys)
    payload = _run(["`git push` two"], monkeypatch, capsys=capsys)
    # Dedup fires (same triggers), action is update
    assert payload["action"] == "updated"
    counts = {
        (t["tool_name"], tuple(t["head"])): t["existing_memories"]
        for t in payload["extracted_triggers"]
        if t["kind"] == "tool_head"
    }
    # Only (git, push) emitted — head-1 suppressed
    assert counts[("Bash", ("git", "push"))] == 1
