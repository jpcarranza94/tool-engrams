"""insert_candidate_triggers drops malformed first_tokens with a stderr warning."""

from __future__ import annotations

import time

from toolengrams.formation.candidates import FormationCandidate
from toolengrams.formation.triggers import (
    first_token_looks_like_cli,
    insert_candidate_triggers,
)


def _seed_memory(conn) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('test-m', '', 'body', 'hint', 'global', NULL, ?)",
        (now_ts,),
    )
    return cur.lastrowid


def test_valid_first_tokens_pass():
    assert first_token_looks_like_cli("git")
    assert first_token_looks_like_cli("aws")
    assert first_token_looks_like_cli("python3")
    assert first_token_looks_like_cli("openai.com")  # WebFetch host
    assert first_token_looks_like_cli("acme-cli")  # hyphenated CLI name
    assert first_token_looks_like_cli("_internal")
    assert first_token_looks_like_cli("jira.example.com")


def test_invalid_first_tokens_rejected():
    assert not first_token_looks_like_cli("--start-time")  # flag
    assert not first_token_looks_like_cli("/opt/agent-service/.env")  # absolute path
    assert not first_token_looks_like_cli(".claude/skills/")  # path fragment
    assert not first_token_looks_like_cli("STAGING_FOO=")  # env-var assignment
    assert not first_token_looks_like_cli("STAGING_FOO=bar")
    assert not first_token_looks_like_cli("with spaces")
    assert not first_token_looks_like_cli("")
    assert not first_token_looks_like_cli(None)


def test_insert_drops_malformed_and_keeps_valid(temp_db, capsys):
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="token_subseq", tokens=("git", "push")),
        FormationCandidate(kind="token_subseq", tokens=("--bogus", "x")),
        FormationCandidate(kind="token_subseq", tokens=("/abs/path", "etc")),
        FormationCandidate(kind="token_subseq", tokens=("aws", "logs")),
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 2  # only git and aws

    rows = temp_db.execute(
        "SELECT first_token FROM triggers WHERE memory_id = ? ORDER BY first_token",
        (mid,),
    ).fetchall()
    assert [r["first_token"] for r in rows] == ["aws", "git"]

    stderr = capsys.readouterr().err
    assert "--bogus" in stderr
    assert "/abs/path" in stderr


def test_path_glob_candidates_unaffected(temp_db, capsys):
    """path_glob triggers don't have first_token; they bypass the new gate."""
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="path_glob", path_pattern="**/*.py"),
        FormationCandidate(kind="path_glob", path_pattern="**/billing/*.py"),
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 2

    rows = temp_db.execute(
        "SELECT path_pattern FROM triggers WHERE memory_id = ? ORDER BY path_pattern",
        (mid,),
    ).fetchall()
    assert [r["path_pattern"] for r in rows] == ["**/*.py", "**/billing/*.py"]
    assert capsys.readouterr().err == ""


def test_empty_tokens_silently_skipped(temp_db, capsys):
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="token_subseq", tokens=()),  # empty
        FormationCandidate(kind="token_subseq", tokens=("git",)),
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 1
    # Empty tokens should NOT produce a stderr warning (it's an old quiet case);
    # only structurally-malformed first_tokens warn.
    err = capsys.readouterr().err
    assert "rejected" not in err
