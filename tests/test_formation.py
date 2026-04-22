"""Unit tests for toolengrams.formation — deterministic trigger extraction."""

from __future__ import annotations

import json

from toolengrams.formation import (
    FormationCandidate,
    consolidate_vocabulary,
    extract_candidates,
    insert_candidate_triggers,
)


def _token_sets(candidates: list[FormationCandidate]) -> set[tuple[str, ...]]:
    return {c.tokens for c in candidates if c.kind == "token_subseq"}


def _globs(candidates: list[FormationCandidate]) -> set[str]:
    return {c.path_pattern for c in candidates if c.kind == "path_glob"}


# ---------- backtick extraction ----------


def test_backtick_single_token_cli():
    c = extract_candidates("Run `mycli` against the replica.")
    assert ("mycli",) in _token_sets(c)


def test_backtick_subcommand_emits_two_token_only():
    """When a second token exists for a subcommand tool, single-token is suppressed."""
    c = extract_candidates("Use `git push origin main` to publish.")
    tokens = _token_sets(c)
    assert ("git", "push") in tokens
    assert ("git",) not in tokens


def test_backtick_subcommand_without_second_token_emits_single():
    """Bare subcommand tool with no args still gets the one-token trigger."""
    c = extract_candidates("Run `git` to see help.")
    assert ("git",) in _token_sets(c)


def test_backtick_non_subcommand_tool_only_single_token():
    c = extract_candidates("`curl https://example.com` fetches it.")
    tokens = _token_sets(c)
    assert ("curl",) in tokens
    # curl is not in _SUBCOMMAND_TOOLS so no two-token trigger
    assert not any(t[0] == "curl" and len(t) == 2 for t in tokens)


def test_backtick_path_snippet_is_not_a_token_trigger():
    c = extract_candidates("Config is at `/etc/foo.conf`.")
    tokens = _token_sets(c)
    assert all(t[0] != "/etc/foo.conf" for t in tokens)


def test_backtick_flag_is_not_a_token_trigger():
    c = extract_candidates("Pass `--verbose` to debug.")
    assert not _token_sets(c)


# ---------- path extraction ----------


def test_absolute_path_emits_exact_and_basename_glob():
    c = extract_candidates("See /home/user/.claude/settings.json for hooks.")
    globs = _globs(c)
    assert "/home/user/.claude/settings.json" in globs
    assert "**/settings.json" in globs


def test_tilde_path_emits_exact():
    c = extract_candidates("Config lives in ~/.zshrc")
    globs = _globs(c)
    assert "~/.zshrc" in globs


def test_glob_pattern_is_preserved():
    c = extract_candidates("Matches **/*.py files")
    globs = _globs(c)
    assert "**/*.py" in globs


def test_path_with_trailing_punctuation_is_stripped():
    c = extract_candidates("See /etc/hosts, or try /var/log/system.log.")
    globs = _globs(c)
    assert "/etc/hosts" in globs
    assert "/var/log/system.log" in globs


# ---------- URL extraction ----------


def test_url_extracts_host_as_single_token_trigger():
    c = extract_candidates("Docs at https://api.github.com/repos/foo/bar for reference.")
    assert ("api.github.com",) in _token_sets(c)


def test_http_and_https_both_extract():
    c = extract_candidates("See http://example.com and https://other.example.com/path")
    tokens = _token_sets(c)
    assert ("example.com",) in tokens
    assert ("other.example.com",) in tokens


# ---------- dedup ----------


def test_duplicate_backticks_dedupe():
    c = extract_candidates("`git push` then `git push` again.")
    hits = [t for t in _token_sets(c) if t == ("git", "push")]
    assert len(hits) == 1


# ---------- consolidation ----------


def test_consolidation_counts_existing_memories(temp_db):
    # Seed an existing memory with a (git, status) trigger so the candidate
    # we're about to extract matches.
    temp_db.execute(
        "INSERT INTO memories (name, body, type, scope, project_slug, created_ts) "
        "VALUES ('seed', 'body', 'reference', 'global', NULL, 1)"
    )
    mid = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', 'git', ?)",
        (mid, json.dumps(["git", "status"])),
    )

    candidates = extract_candidates("Use `git status` to check")
    annotated = consolidate_vocabulary(temp_db, candidates)

    by_tokens = {c.tokens: c.existing_memories for c in annotated if c.kind == "token_subseq"}
    assert ("git",) not in by_tokens  # suppressed (two-token extracted instead)
    assert by_tokens[("git", "status")] == 1


def test_consolidation_path_glob(temp_db):
    temp_db.execute(
        "INSERT INTO memories (name, body, type, scope, project_slug, created_ts) "
        "VALUES ('seed', 'body', 'reference', 'global', NULL, 1)"
    )
    mid = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) "
        "VALUES (?, 'path_glob', '**/settings.json')",
        (mid,),
    )

    candidates = extract_candidates("See ~/.claude/settings.json")
    annotated = consolidate_vocabulary(temp_db, candidates)

    by_pat = {c.path_pattern: c.existing_memories for c in annotated if c.kind == "path_glob"}
    assert by_pat["**/settings.json"] == 1
    assert by_pat["~/.claude/settings.json"] == 0


# ---------- insert helper ----------


def test_insert_candidate_triggers_writes_rows(temp_db):
    temp_db.execute(
        "INSERT INTO memories (name, body, type, scope, project_slug, created_ts) "
        "VALUES ('m', 'body', 'reference', 'global', NULL, 1)"
    )
    mid = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    candidates = [
        FormationCandidate(kind="token_subseq", tokens=("git", "push"), source="backtick"),
        FormationCandidate(kind="path_glob", path_pattern="**/*.py", source="path"),
    ]
    n = insert_candidate_triggers(temp_db, mid, candidates)
    assert n == 2

    rows = temp_db.execute(
        "SELECT kind, first_token, tokens_json, path_pattern "
        "FROM triggers WHERE memory_id = ? ORDER BY id",
        (mid,),
    ).fetchall()
    assert rows[0]["kind"] == "token_subseq"
    assert rows[0]["first_token"] == "git"
    assert json.loads(rows[0]["tokens_json"]) == ["git", "push"]
    assert rows[1]["kind"] == "path_glob"
    assert rows[1]["path_pattern"] == "**/*.py"
