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
    assert payload["inserted"] is True
    assert payload["memory"]["id"] is not None
    rows = _rows(temp_db, "SELECT name, body, type, scope FROM memories")
    assert len(rows) == 1
    assert "git status" in rows[0]["body"]


def test_stdin_body_when_text_is_dash(temp_db, monkeypatch, capsys):
    payload = _run(["-"], monkeypatch, stdin="body via stdin `mycli`\n", capsys=capsys)
    assert payload["inserted"] is True
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
    long = "x" * 200
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
    assert ("Bash", ("git",)) in heads
    assert ("Bash", ("git", "push")) in heads
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
    assert ("tool_head", "Bash", "git", 1) in kinds
    assert ("tool_head", "Bash", "git push", 2) in kinds


# ---------- extra triggers ----------


def test_extra_trigger_keyword(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--extra-trigger", "keyword:mycli", "body text"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["extra_triggers"] == [{"kind": "keyword", "keyword": "mycli"}]
    rows = _rows(temp_db, "SELECT kind, keyword FROM triggers WHERE kind='keyword'")
    assert rows[0]["keyword"] == "mycli"


def test_extra_trigger_tool_head(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--extra-trigger", "tool_head:Bash:git,push", "body"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["extra_triggers"][0]["kind"] == "tool_head"
    # JSON round-trips tuples to lists.
    assert list(payload["extra_triggers"][0]["head"]) == ["git", "push"]


def test_extra_trigger_error_contains(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--extra-trigger", "error_contains:Bash:ssh:Connection refused", "body"],
        monkeypatch,
        capsys=capsys,
    )
    rows = _rows(
        temp_db,
        "SELECT tool_name, head_joined, error_substring FROM triggers WHERE kind='error_contains'",
    )
    assert rows[0]["tool_name"] == "Bash"
    assert rows[0]["head_joined"] == "ssh"
    assert rows[0]["error_substring"] == "Connection refused"


def test_extra_trigger_malformed_raises(temp_db, monkeypatch):
    with pytest.raises(SystemExit):
        remember.main(["--extra-trigger", "nonsense", "body"])


# ---------- dry run ----------


def test_dry_run_does_not_insert(temp_db, monkeypatch, capsys):
    payload = _run(["--dry-run", "`git push`"], monkeypatch, capsys=capsys)
    assert payload["inserted"] is False
    assert payload["dry_run"] is True
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
    payload = _run(["body"], monkeypatch, capsys=capsys)
    assert payload["memory"]["project_slug"] == "-tmp-fake-project"


def test_scope_project_with_override(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--scope", "project", "--project-slug", "custom-slug", "body"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["project_slug"] == "custom-slug"


def test_invalid_type_returns_2(temp_db, monkeypatch, capsys):
    rc = remember.main(["--type", "bogus", "body"])
    assert rc == 2


def test_pinned_flag_stored(temp_db, monkeypatch, capsys):
    _run(["--pinned", "body"], monkeypatch, capsys=capsys)
    rows = _rows(temp_db, "SELECT pinned FROM memories")
    assert rows[0]["pinned"] == 1


# ---------- vocabulary consolidation ----------


def test_consolidation_counts_after_second_insert(temp_db, monkeypatch, capsys):
    _run(["`git push` one"], monkeypatch, capsys=capsys)
    payload = _run(["`git push` two"], monkeypatch, capsys=capsys)
    # The second call should see 1 existing memory for each head.
    counts = {
        (t["tool_name"], tuple(t["head"])): t["existing_memories"]
        for t in payload["extracted_triggers"]
        if t["kind"] == "tool_head"
    }
    assert counts[("Bash", ("git",))] == 1
    assert counts[("Bash", ("git", "push"))] == 1
