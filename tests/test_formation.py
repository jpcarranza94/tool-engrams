"""Unit tests for toolengrams.formation — deterministic trigger extraction."""

from __future__ import annotations

from toolengrams.formation import (
    FormationCandidate,
    consolidate_vocabulary,
    extract_candidates,
)
from toolengrams.triggers import insert_candidate_triggers


def _heads(candidates: list[FormationCandidate], tool: str) -> set[tuple[str, ...]]:
    return {c.head for c in candidates if c.kind == "tool_head" and c.tool_name == tool}


def _globs(candidates: list[FormationCandidate]) -> set[str]:
    return {c.path_pattern for c in candidates if c.kind == "path_glob"}


# ---------- backtick extraction ----------


def test_backtick_single_token_cli_emits_head1():
    c = extract_candidates("Run `mycli` against the replica.")
    assert ("mycli",) in _heads(c, "Bash")


def test_backtick_subcommand_emits_head2_only():
    """When head-2 exists, head-1 is suppressed (too broad)."""
    c = extract_candidates("Use `git push origin main` to publish.")
    heads = _heads(c, "Bash")
    assert ("git", "push") in heads
    assert ("git",) not in heads  # suppressed — git alone is too noisy


def test_backtick_subcommand_without_head2_emits_head1():
    """Bare subcommand tool with no args still gets head-1."""
    c = extract_candidates("Run `git` to see help.")
    heads = _heads(c, "Bash")
    assert ("git",) in heads


def test_backtick_non_subcommand_tool_only_emits_head1():
    c = extract_candidates("`curl https://example.com` fetches it.")
    heads = _heads(c, "Bash")
    assert ("curl",) in heads
    # curl is not in _SUBCOMMAND_TOOLS so no head2
    assert not any(h[0] == "curl" and len(h) == 2 for h in heads)


def test_backtick_path_snippet_is_not_treated_as_tool_head():
    c = extract_candidates("Config is at `/etc/foo.conf`.")
    heads = _heads(c, "Bash")
    assert all(h[0] != "/etc/foo.conf" for h in heads)


def test_backtick_flag_is_not_treated_as_tool_head():
    c = extract_candidates("Pass `--verbose` to debug.")
    heads = _heads(c, "Bash")
    assert not heads  # --verbose starts with '-', filtered


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


def test_url_extracts_host_as_webfetch_head():
    c = extract_candidates("Docs at https://api.github.com/repos/foo/bar for reference.")
    assert ("api.github.com",) in _heads(c, "WebFetch")


def test_http_and_https_both_extract():
    c = extract_candidates("See http://example.com and https://other.example.com/path")
    heads = _heads(c, "WebFetch")
    assert ("example.com",) in heads
    assert ("other.example.com",) in heads


# ---------- dedup ----------


def test_duplicate_backticks_dedupe():
    c = extract_candidates("`git push` then `git push` again.")
    heads = [h for h in _heads(c, "Bash") if h == ("git", "push")]
    assert len(heads) == 1


# ---------- consolidation ----------


def test_consolidation_counts_existing_memories(temp_db):
    # Seed an existing memory with a (Bash, git) trigger.
    temp_db.execute(
        "INSERT INTO memories (name, body, type, scope, project_slug, created_ts) "
        "VALUES ('seed', 'body', 'reference', 'global', NULL, 1)"
    )
    mid = temp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
        "VALUES (?, 'tool_head', 'Bash', 'git', 1)",
        (mid,),
    )

    candidates = extract_candidates("Use `git status` to check")
    annotated = consolidate_vocabulary(temp_db, candidates)

    # Only (git, status) is emitted now (head-1 suppressed). It has 0 existing matches.
    by_head = {c.head: c.existing_memories for c in annotated if c.kind == "tool_head"}
    assert ("git",) not in by_head  # suppressed
    assert by_head[("git", "status")] == 0


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
        FormationCandidate(kind="tool_head", tool_name="Bash", head=("git", "push"), source="backtick"),
        FormationCandidate(kind="path_glob", path_pattern="**/*.py", source="path"),
    ]
    n = insert_candidate_triggers(temp_db, mid, candidates)
    assert n == 2

    rows = temp_db.execute(
        "SELECT kind, tool_name, head_joined, head_length, path_pattern "
        "FROM triggers WHERE memory_id = ? ORDER BY id",
        (mid,),
    ).fetchall()
    assert rows[0]["kind"] == "tool_head"
    assert rows[0]["tool_name"] == "Bash"
    assert rows[0]["head_joined"] == "git push"
    assert rows[0]["head_length"] == 2
    assert rows[1]["kind"] == "path_glob"
    assert rows[1]["path_pattern"] == "**/*.py"
