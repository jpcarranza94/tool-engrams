"""insert_candidate_triggers drops malformed first_tokens with a stderr warning."""

from __future__ import annotations

import json
import time

from toolengrams.formation.candidates import FormationCandidate
from toolengrams.formation.triggers import (
    first_token_looks_like_cli,
    insert_candidate_triggers,
    path_glob_is_specific_enough,
    token_trigger_is_specific_enough,
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
    """Directory-qualified path globs bypass the first_token CLI gate and the
    specificity gate; they don't have a first_token and aren't broad."""
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="path_glob", path_pattern="**/billing/models.py"),
        FormationCandidate(kind="path_glob", path_pattern="**/billing/*.py"),
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 2

    rows = temp_db.execute(
        "SELECT path_pattern FROM triggers WHERE memory_id = ? ORDER BY path_pattern",
        (mid,),
    ).fetchall()
    assert [r["path_pattern"] for r in rows] == ["**/billing/*.py", "**/billing/models.py"]
    assert capsys.readouterr().err == ""


def test_empty_tokens_silently_skipped(temp_db, capsys):
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="token_subseq", tokens=()),  # empty
        FormationCandidate(kind="token_subseq", tokens=("git", "status")),
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 1
    # Empty tokens should NOT produce a stderr warning (it's an old quiet case);
    # only structurally-malformed or too-broad first_tokens warn.
    err = capsys.readouterr().err
    assert "rejected" not in err


# ---------- WS3.3: minimum trigger specificity at formation ----------


def test_token_specificity_predicate_rejects_bare_subcommand_tools():
    # Single-token subcommand tools over-match: refused.
    for tool in ("git", "gh", "jira", "ssh", "aws", "docker", "kubectl",
                 "npm", "yarn", "psql", "make", "gcloud", "bq", "systemctl",
                 "cargo"):
        assert not token_trigger_is_specific_enough((tool,)), tool


def test_token_specificity_predicate_allows_two_token_subcommand():
    assert token_trigger_is_specific_enough(("gh", "pr"))
    assert token_trigger_is_specific_enough(("git", "push", "--force"))


def test_token_specificity_predicate_allows_single_no_subcommand_tool():
    # Simple no-subcommand tools and URL hosts stay legal as single tokens.
    assert token_trigger_is_specific_enough(("ergdb",))
    assert token_trigger_is_specific_enough(("curl",))
    assert token_trigger_is_specific_enough(("openai.com",))
    assert token_trigger_is_specific_enough(("jenkins.example.com",))


def test_insert_drops_bare_subcommand_token_keeps_specific(temp_db, capsys):
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="token_subseq", tokens=("gh",)),          # too broad
        FormationCandidate(kind="token_subseq", tokens=("jira",)),        # too broad
        FormationCandidate(kind="token_subseq", tokens=("gh", "pr")),     # specific
        FormationCandidate(kind="token_subseq", tokens=("ergdb",)),       # no-subcmd tool
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 2

    rows = temp_db.execute(
        "SELECT tokens_json FROM triggers WHERE memory_id = ? ORDER BY first_token",
        (mid,),
    ).fetchall()
    kept = {tuple(json.loads(r["tokens_json"])) for r in rows}
    assert kept == {("ergdb",), ("gh", "pr")}

    err = capsys.readouterr().err
    assert "'gh'" in err and "'jira'" in err
    assert "too broad" in err


def test_path_specificity_predicate_rejects_broad_globs():
    assert not path_glob_is_specific_enough("**/*.py")       # extension-only
    assert not path_glob_is_specific_enough("**/*.json")
    assert not path_glob_is_specific_enough("*.ts")          # bare extension-only
    assert not path_glob_is_specific_enough("**/*")          # match-all
    assert not path_glob_is_specific_enough("**")
    assert not path_glob_is_specific_enough("**/__init__.py")  # common basename
    assert not path_glob_is_specific_enough("**/settings.json")
    # Case-insensitive basename match: the set stores lowercase, the predicate
    # lowercases the basename — pins the `.lower()` against deletion.
    assert not path_glob_is_specific_enough("**/Dockerfile")
    assert not path_glob_is_specific_enough("**/Makefile")


def test_path_specificity_predicate_allows_narrow_patterns():
    assert path_glob_is_specific_enough("**/billing/models.py")  # dir-qualified
    assert path_glob_is_specific_enough("**/billing/*.py")
    assert path_glob_is_specific_enough("/repo/src/app.py")      # rooted exact
    assert path_glob_is_specific_enough("~/.gitconfig")
    assert path_glob_is_specific_enough("**/custom_unique_name.py")  # uncommon basename


def test_insert_drops_broad_path_globs_keeps_narrow(temp_db, capsys):
    mid = _seed_memory(temp_db)
    cands = [
        FormationCandidate(kind="path_glob", path_pattern="**/*.py"),          # broad
        FormationCandidate(kind="path_glob", path_pattern="**/__init__.py"),   # broad
        FormationCandidate(kind="path_glob", path_pattern="**/billing/models.py"),  # ok
    ]
    n = insert_candidate_triggers(temp_db, mid, cands)
    assert n == 1

    rows = temp_db.execute(
        "SELECT path_pattern FROM triggers WHERE memory_id = ?", (mid,),
    ).fetchall()
    assert [r["path_pattern"] for r in rows] == ["**/billing/models.py"]

    err = capsys.readouterr().err
    assert "**/*.py" in err and "too broad" in err
