"""`engram trigger <memory_id>` — add/remove/list triggers on an existing
memory without recreating it (counters preserved). Consolidation's narrowing
lever for noisy (over-matching) triggers.
"""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import trigger


def _mem_with_trigger(conn, *, name="m", useful=4, noise=1):
    mid = memory_store.insert_memory(
        conn, name=name, description="", body="b", kind="hint",
        scope="global", project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    # Carry some reinforcement so we can prove it survives trigger surgery.
    conn.execute("UPDATE memories SET useful_count = ?, noise_count = ? WHERE id = ?",
                 (useful, noise, mid))
    memory_store.add_path_trigger(conn, mid, "**/Dockerfile")
    return mid


def _triggers(conn, mid):
    return memory_store.triggers_for(conn, mid)


def test_list_triggers(temp_db, capsys):
    mid = _mem_with_trigger(temp_db)
    rc = trigger.main([str(mid), "--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "list"
    assert out["triggers"][0]["path_pattern"] == "**/Dockerfile"


def test_add_token_trigger(temp_db, capsys):
    mid = _mem_with_trigger(temp_db)
    rc = trigger.main([str(mid), "--add-trigger", "docker build"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "updated"
    assert out["added"] == 1
    kinds = {t.kind for t in _triggers(temp_db, mid)}
    assert kinds == {"path_glob", "token_subseq"}


def test_add_path_trigger_with_access_mode(temp_db, capsys):
    """--access-mode tags added path globs; the default Dockerfile glob stays write."""
    mid = _mem_with_trigger(temp_db)
    rc = trigger.main([str(mid), "--add-path", "**/*.py", "--access-mode", "any"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "updated"
    modes = {t.path_pattern: t.access_mode for t in _triggers(temp_db, mid)}
    assert modes["**/*.py"] == "any"
    assert modes["**/Dockerfile"] == "write"


def test_list_shows_access_mode(temp_db, capsys):
    mid = _mem_with_trigger(temp_db)
    trigger.main([str(mid), "--list"])
    out = json.loads(capsys.readouterr().out)
    path_trig = [t for t in out["triggers"] if t["kind"] == "path_glob"][0]
    assert path_trig["access_mode"] == "write"


def test_narrow_glob_replace_in_one_call(temp_db, capsys):
    """Remove a broad glob and add a narrow one — the noise-fix path."""
    mid = _mem_with_trigger(temp_db)
    broad_id = _triggers(temp_db, mid)[0].id

    rc = trigger.main([str(mid), "--remove", str(broad_id),
                       "--add-path", "infra/**/Dockerfile"])
    assert rc == 0
    paths = [t.path_pattern for t in _triggers(temp_db, mid)]
    assert paths == ["infra/**/Dockerfile"]
    # Reinforcement counters are untouched by trigger surgery.
    row = temp_db.execute(
        "SELECT useful_count, noise_count FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    assert (row["useful_count"], row["noise_count"]) == (4, 1)


def test_remove_only_trigger_is_rejected(temp_db, capsys):
    """Refuse to orphan a memory — a triggerless memory can never surface."""
    mid = _mem_with_trigger(temp_db)
    only_id = _triggers(temp_db, mid)[0].id

    rc = trigger.main([str(mid), "--remove", str(only_id)])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "would_orphan"
    # The trigger is still there (transaction rolled back).
    assert len(_triggers(temp_db, mid)) == 1


def test_remove_unknown_trigger_id_rejected(temp_db, capsys):
    mid = _mem_with_trigger(temp_db)
    rc = trigger.main([str(mid), "--remove", "99999"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_a_trigger_of_memory"


def test_unknown_memory_rejected(temp_db, capsys):
    rc = trigger.main(["4242", "--list"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_found"


def test_malformed_trigger_rejected_at_chokepoint(temp_db, capsys):
    """A trigger whose first token can't be a shell command head is dropped by
    insert_candidate_triggers; the existing path-glob still satisfies the
    orphan guard, so the call succeeds with added=0."""
    mid = _mem_with_trigger(temp_db)
    rc = trigger.main([str(mid), "--add-trigger", "--flag only"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["add_requested"] == 1
    assert out["added"] == 0   # rejected: first token '--flag' isn't a command head
