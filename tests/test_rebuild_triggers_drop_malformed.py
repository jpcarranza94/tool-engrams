"""`engram rebuild-triggers --drop-malformed` removes structurally-impossible
first_token rows and re-derives from body when a memory is left orphaned."""

from __future__ import annotations

import json
import time

from toolengrams.cli import rebuild_triggers


def _seed_memory(conn, name: str, body: str = "body") -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, 'hint', 'global', NULL, ?)",
        (name, body, now_ts),
    )
    return cur.lastrowid


def _seed_trigger(conn, memory_id: int, first_token: str, tokens: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', ?, ?)",
        (memory_id, first_token, json.dumps(tokens)),
    )
    return cur.lastrowid


def test_drops_malformed_keeps_valid(temp_db, capsys):
    """Memory with one valid + two malformed triggers: only malformed dropped."""
    mid = _seed_memory(temp_db, "mixed")
    _seed_trigger(temp_db, mid, "git", ["git", "push"])
    _seed_trigger(temp_db, mid, "STAGING_FOO=", ["STAGING_FOO="])
    _seed_trigger(temp_db, mid, "/abs/path", ["/abs/path", "etc"])

    rc = rebuild_triggers.main(["--drop-malformed"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["malformed_triggers_found"] == 2
    assert out["trigger_rows_dropped"] == 2
    assert out["memories_orphaned"] == 0

    rows = temp_db.execute(
        "SELECT first_token FROM triggers WHERE memory_id = ? ORDER BY first_token",
        (mid,),
    ).fetchall()
    assert [r["first_token"] for r in rows] == ["git"]


def test_dry_run_does_not_modify(temp_db, capsys):
    mid = _seed_memory(temp_db, "dry")
    _seed_trigger(temp_db, mid, "STAGING_FOO=", ["STAGING_FOO="])

    rc = rebuild_triggers.main(["--drop-malformed", "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["malformed_triggers_found"] == 1
    assert out["mode"] == "dry_run"

    # Row still there.
    cnt = temp_db.execute(
        "SELECT COUNT(*) FROM triggers WHERE memory_id = ?", (mid,)
    ).fetchone()[0]
    assert cnt == 1


def test_rebuilds_from_body_when_orphaned(temp_db, capsys):
    """Memory whose ONLY triggers are malformed: drop, then re-derive from body."""
    body = "Without this memory, Claude would forget to use `git push --force-with-lease`."
    mid = _seed_memory(temp_db, "orphan-then-rebuild", body=body)
    _seed_trigger(temp_db, mid, "STAGING_FOO=", ["STAGING_FOO="])

    rc = rebuild_triggers.main(["--drop-malformed"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["malformed_triggers_found"] == 1
    assert out["memories_orphaned"] == 1
    assert out["memories_rebuilt_from_body"] == 1

    rows = temp_db.execute(
        "SELECT first_token FROM triggers WHERE memory_id = ?", (mid,)
    ).fetchall()
    assert len(rows) >= 1
    assert any(r["first_token"] == "git" for r in rows)


def test_orphaned_with_no_body_extraction_reported(temp_db, capsys):
    body = "Some prose with no backticked code or paths or URLs."
    mid = _seed_memory(temp_db, "stays-orphaned", body=body)
    _seed_trigger(temp_db, mid, "STAGING_FOO=", ["STAGING_FOO="])

    rc = rebuild_triggers.main(["--drop-malformed"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["memories_orphaned"] == 1
    assert out["memories_rebuilt_from_body"] == 0
    assert any(m["id"] == mid for m in out["memories_still_orphaned"])


def test_no_malformed_returns_clean(temp_db, capsys):
    mid = _seed_memory(temp_db, "all-good")
    _seed_trigger(temp_db, mid, "git", ["git", "push"])

    rc = rebuild_triggers.main(["--drop-malformed"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["malformed_triggers_found"] == 0
    assert out["trigger_rows_dropped"] == 0


def test_does_not_touch_path_glob_rows(temp_db, capsys):
    mid = _seed_memory(temp_db, "path-only")
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) "
        "VALUES (?, 'path_glob', '**/*.py')",
        (mid,),
    )

    rc = rebuild_triggers.main(["--drop-malformed"])
    assert rc == 0

    rows = temp_db.execute(
        "SELECT kind, path_pattern FROM triggers WHERE memory_id = ?", (mid,)
    ).fetchall()
    assert rows[0]["kind"] == "path_glob"
    assert rows[0]["path_pattern"] == "**/*.py"
