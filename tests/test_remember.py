"""Unit tests for the `engram remember` CLI handler."""

from __future__ import annotations

import io
import json

import pytest

from toolengrams.cli import remember


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
    rows = _rows(temp_db, "SELECT name, body, kind, scope FROM memories")
    assert len(rows) == 1
    assert "git status" in rows[0]["body"]


def test_stdin_body_when_text_is_dash(temp_db, monkeypatch, capsys):
    # Use a two-token command: a bare `psql` reduces to a single subcommand-tool
    # token, which the specificity gate now refuses (would be trigger-less).
    payload = _run(["-"], monkeypatch, stdin="body via stdin `git status`\n", capsys=capsys)
    assert payload["action"] == "inserted"
    rows = _rows(temp_db, "SELECT body FROM memories")
    assert rows[0]["body"].startswith("body via stdin")


def test_empty_body_returns_exit_2(temp_db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = remember.main([])
    assert rc == 2


def test_path_access_mode_applied_to_explicit_path(temp_db, monkeypatch, capsys):
    # Directory-qualified glob (the specificity gate refuses extension-only ones).
    payload = _run(
        ["--path", "**/billing/models.py", "--access-mode", "read", "body about billing"],
        monkeypatch, capsys=capsys,
    )
    assert payload["action"] == "inserted"
    modes = {r["path_pattern"]: r["access_mode"]
             for r in _rows(temp_db,
                            "SELECT path_pattern, access_mode FROM triggers "
                            "WHERE kind='path_glob'")}
    assert modes["**/billing/models.py"] == "read"


def test_path_access_mode_defaults_to_write(temp_db, monkeypatch, capsys):
    _run(["--path", "**/billing/models.py", "body"], monkeypatch, capsys=capsys)
    row = _rows(temp_db, "SELECT access_mode FROM triggers WHERE kind='path_glob'")[0]
    assert row["access_mode"] == "write"


def test_access_mode_applies_to_body_extracted_paths(temp_db, monkeypatch, capsys):
    _run(["--access-mode", "any", "Reading ~/.config/foo.toml is safe"],
         monkeypatch, capsys=capsys)
    rows = _rows(temp_db,
                 "SELECT access_mode FROM triggers WHERE kind='path_glob'")
    assert rows
    assert all(r["access_mode"] == "any" for r in rows)


def test_name_synthesized_from_first_line(temp_db, monkeypatch, capsys):
    body = "First line is the synthesized name\nSecond line has more context `git push`."
    payload = _run([body], monkeypatch, capsys=capsys)
    assert payload["memory"]["name"] == "First line is the synthesized name"


def test_name_override_respected(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--name", "custom name", "body with `git push`"],
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

    token_triggers = {
        tuple(t["tokens"])
        for t in payload["extracted_triggers"]
        if t["kind"] == "token_subseq"
    }
    assert ("git", "push") in token_triggers
    assert ("git",) not in token_triggers  # single-token suppressed when two-token exists
    assert ("example.com",) in token_triggers

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
        "SELECT kind, first_token, tokens_json FROM triggers WHERE kind = 'token_subseq' ORDER BY id",
    )
    triggers = [(r["kind"], r["first_token"], json.loads(r["tokens_json"])) for r in rows]
    assert ("token_subseq", "git", ["git", "push"]) in triggers
    # Single-token (["git"]) is suppressed when the two-token form is extracted.
    assert not any(t[2] == ["git"] for t in triggers)


# ---------- extra triggers ----------


def test_extra_trigger_keyword_rejected(temp_db, monkeypatch):
    """keyword triggers are not supported."""
    with pytest.raises(SystemExit):
        remember.main(["--extra-trigger", "keyword:psql", "body text"])


def test_extra_trigger_token_subseq(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--extra-trigger", "token_subseq:git,push", "some `git` body"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["extra_triggers"][0]["kind"] == "token_subseq"
    assert list(payload["extra_triggers"][0]["tokens"]) == ["git", "push"]


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


# ---------- scope / kind ----------


def test_scope_global_stores_null_project_slug(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--scope", "global", "body with `git push`"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["project_slug"] is None
    rows = _rows(temp_db, "SELECT scope, project_slug FROM memories")
    assert rows[0]["scope"] == "global"
    assert rows[0]["project_slug"] is None


def test_scope_project_defaults_slug_from_cwd_flag(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--project-cwd", "/tmp/fake/project", "use `make build` here"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["project_slug"] == "-tmp-fake-project"


def test_scope_project_defaults_slug_from_real_cwd(
    temp_db, monkeypatch, capsys, tmp_path,
):
    """Without --project-cwd, fall back to os.getcwd()."""
    monkeypatch.chdir(tmp_path)
    payload = _run(["use `make build` here"], monkeypatch, capsys=capsys)
    expected = str(tmp_path).replace("/", "-")
    assert payload["memory"]["project_slug"] == expected


def test_scope_project_with_override(temp_db, monkeypatch, capsys):
    payload = _run(
        ["--scope", "project", "--project-slug", "custom-slug", "use `make test`"],
        monkeypatch,
        capsys=capsys,
    )
    assert payload["memory"]["project_slug"] == "custom-slug"


def test_invalid_kind_returns_2(temp_db, monkeypatch, capsys):
    rc = remember.main(["--kind", "bogus", "body"])
    assert rc == 2


def test_pinned_flag_stored(temp_db, monkeypatch, capsys):
    _run(["--pinned", "use `make deploy` carefully"], monkeypatch, capsys=capsys)
    rows = _rows(temp_db, "SELECT pinned FROM memories")
    assert rows[0]["pinned"] == 1


# ---------- dedup ----------


def test_dedup_collision_withholds_for_review(temp_db, monkeypatch, capsys):
    """A second memory sharing a trigger is WITHHELD for review — it must NOT
    silently overwrite the existing memory (the memory-137 data-loss bug)."""
    p1 = _run(["`git push` -- always force push"], monkeypatch, capsys=capsys)
    assert p1["action"] == "inserted"
    mid = p1["memory"]["id"]
    before = _rows(temp_db, "SELECT body FROM memories WHERE id = ?", mid)[0]["body"]

    p2 = _run(["`git push` -- never force push actually"], monkeypatch, capsys=capsys)
    assert p2["action"] == "review_collision"          # NOT updated, NOT inserted
    assert p2["collision"]["id"] == mid
    assert "git push" in " ".join(p2["collision"]["shared_triggers"])
    assert p2["guidance"]["recommended"] in {"fold", "keep_both"}

    # Nothing was written: still one memory, victim body untouched.
    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 1
    after = _rows(temp_db, "SELECT body FROM memories WHERE id = ?", mid)[0]["body"]
    assert after == before
    assert "always force push" in after                # original preserved


def test_dedup_allows_distinct_memories(temp_db, monkeypatch, capsys):
    """Memories with different triggers should both insert."""
    _run(["`git push` rule"], monkeypatch, capsys=capsys)
    p2 = _run(["`docker compose up` rule"], monkeypatch, capsys=capsys)
    assert p2["action"] == "inserted"

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 2


def test_dedup_collision_different_bodies_recommends_keep_both(temp_db, monkeypatch, capsys):
    """Same trigger but genuinely DIFFERENT facts → withhold and lead with the
    keep-both recommendation (folding two different facts is its own data-loss)."""
    p1 = _run(["--name", "git push rule", "`git push` -- always target the origin remote"],
              monkeypatch, capsys=capsys)
    mid = p1["memory"]["id"]
    before = _rows(temp_db, "SELECT body FROM memories WHERE id = ?", mid)[0]["body"]
    p2 = _run(["--name", "git push updated", "`git push` -- run the linter beforehand"],
              monkeypatch, capsys=capsys)
    assert p2["action"] == "review_collision"
    assert p2["guidance"]["recommended"] == "keep_both"

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 1                            # nothing new written
    after = _rows(temp_db, "SELECT body FROM memories WHERE id = ?", mid)[0]["body"]
    assert after == before                             # victim untouched — no overwrite


def test_dedup_collision_near_dup_bodies_recommends_fold(temp_db, monkeypatch, capsys):
    """Same trigger AND near-duplicate bodies → lead with the fold recommendation."""
    body = ("Without this memory the agent would `git push` to a shared branch and "
            "clobber a teammate; always use with-lease to stay safe")
    p1 = _run(["--name", "git-push-lease", body], monkeypatch, capsys=capsys)
    mid = p1["memory"]["id"]
    p2 = _run(["--name", "git-push-lease-again", body + " and coordinate first"],
              monkeypatch, capsys=capsys)
    assert p2["action"] == "review_collision"
    assert p2["guidance"]["recommended"] == "fold"
    assert p2["collision"]["similarity"] >= 0.6

    # Even the fold recommendation is non-writing — the victim is untouched.
    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 1
    assert body in _rows(temp_db, "SELECT body FROM memories WHERE id = ?", mid)[0]["body"]


def test_dedup_collision_folds_when_victim_outside_similar_window(
        temp_db, monkeypatch, capsys):
    """The fold recommendation scores the colliding pair DIRECTLY (score_pair),
    not through a find_similar top-N window. A near-duplicate victim must still be
    recommended for fold even when many other memories rank ahead of it in a text
    search — the regression the score_pair switch (commit 2) fixed. Under the old
    find_similar(limit=10) window the distractors below crowded the victim out to
    a spurious 0.0 → keep_both."""
    new_body = ("the deploy script must export FOO before the migration or the "
                "database ends up half migrated and needs manual repair")
    v_body = ("the deploy script must export FOO before the migration or the "
              "database ends up half migrated")
    # Victim shares the trigger and is a near-duplicate of new_body (~0.7).
    p1 = _run([v_body, "--name", "victim", "--trigger", "deploy foo"],
              monkeypatch, capsys=capsys)
    vid = p1["memory"]["id"]
    # 12 higher-ranked distractors: body identical to new_body (Jaccard ~0.86 vs
    # ~0.70 for the victim), each on its own trigger, forced past the semantic gate.
    for i in range(12):
        _run([new_body, "--name", f"noise-{i}", "--trigger", f"noisetok{i} alpha",
              "--force"], monkeypatch, capsys=capsys)

    p2 = _run([new_body, "--name", "newmem", "--trigger", "deploy foo"],
              monkeypatch, capsys=capsys)
    assert p2["action"] == "review_collision"
    assert p2["collision"]["id"] == vid                # the victim, not a distractor
    assert p2["collision"]["similarity"] >= 0.6
    assert p2["guidance"]["recommended"] == "fold"     # NOT the spurious keep_both


def test_dedup_collision_on_path_glob(temp_db, monkeypatch, capsys):
    """Trigger collision also fires for path_glob triggers, and the shared glob is
    surfaced in shared_triggers (the token_subseq path isn't the only one gated)."""
    # Directory-qualified glob: a bare `**/Makefile` is refused by the WS3.3
    # specificity gate (common basename), so use one that survives to reach dedup.
    p1 = _run(["--path", "**/infra/Makefile", "always use tabs, never spaces, in the Makefile"],
              monkeypatch, capsys=capsys)
    assert p1["action"] == "inserted"
    mid = p1["memory"]["id"]

    p2 = _run(["--path", "**/infra/Makefile", "run make check before every commit"],
              monkeypatch, capsys=capsys)
    assert p2["action"] == "review_collision"
    assert p2["collision"]["id"] == mid
    assert "**/infra/Makefile" in p2["collision"]["shared_triggers"]

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 1                            # withheld, nothing written


def test_force_creates_distinct_memory_sharing_trigger(temp_db, monkeypatch, capsys):
    """--force bypasses the collision gate: a distinct new memory is inserted even
    though it shares a trigger with an existing one."""
    p1 = _run(["`git push` -- always force push"], monkeypatch, capsys=capsys)
    mid = p1["memory"]["id"]

    p2 = _run(["--force", "`git push` -- also run the tests first"], monkeypatch, capsys=capsys)
    assert p2["action"] == "inserted"
    assert p2["memory"]["id"] != mid                   # distinct row, not an overwrite

    rows = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert rows[0]["c"] == 2


def test_into_folds_counter_preservingly(temp_db, monkeypatch, capsys):
    """--into <id> still folds explicitly, preserving the target's counters."""
    p1 = _run(["--name", "git-push-rule", "`git push` -- always force push"],
              monkeypatch, capsys=capsys)
    mid = p1["memory"]["id"]
    temp_db.execute("UPDATE memories SET useful_count = 3 WHERE id = ?", (mid,))
    temp_db.commit()

    p2 = _run(["--into", str(mid), "--name", "git-push-rule",
               "`git push` -- merged: force push only with lease"],
              monkeypatch, capsys=capsys)
    assert p2["action"] == "merged_into"
    assert p2["merged_into"] == mid

    rows = _rows(temp_db,
                 "SELECT useful_count, body FROM memories WHERE id = ?", mid)
    assert rows[0]["useful_count"] == 3                # counters preserved
    assert "merged" in rows[0]["body"]
    cnt = _rows(temp_db, "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL")
    assert cnt[0]["c"] == 1                            # folded, no new row


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


def test_consolidation_counts_on_forced_insert(temp_db, monkeypatch, capsys):
    _run(["`git push` one"], monkeypatch, capsys=capsys)
    # --force to insert a distinct memory sharing the trigger; vocabulary
    # consolidation still reports the pre-existing memory that uses `git push`.
    payload = _run(["--force", "`git push` two"], monkeypatch, capsys=capsys)
    assert payload["action"] == "inserted"
    counts = {
        tuple(t["tokens"]): t["existing_memories"]
        for t in payload["extracted_triggers"]
        if t["kind"] == "token_subseq"
    }
    assert counts[("git", "push")] == 1
