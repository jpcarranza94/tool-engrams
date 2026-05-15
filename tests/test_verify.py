"""`engram verify <name>` — mark memory as still accurate (last_verified_ts = NOW)."""

from __future__ import annotations

import json
import time

from toolengrams.cli import verify


def _seed_memory(conn, name: str, archived: bool = False) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts, archived_ts) "
        "VALUES (?, '', 'body', 'hint', 'global', NULL, ?, ?)",
        (name, now_ts, now_ts if archived else None),
    )
    return cur.lastrowid


def test_verify_sets_last_verified_ts(temp_db, capsys):
    mid = _seed_memory(temp_db, "test-memory")

    rc = verify.main(["test-memory"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "verified"
    assert payload["memory_id"] == mid
    assert payload["previous_last_verified_ts"] is None

    row = temp_db.execute(
        "SELECT last_verified_ts FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    assert row["last_verified_ts"] is not None
    assert row["last_verified_ts"] >= int(time.time()) - 5


def test_verify_unknown_memory_errors_cleanly(temp_db, capsys):
    rc = verify.main(["does-not-exist"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


def test_verify_refuses_archived_memory(temp_db, capsys):
    _seed_memory(temp_db, "archived-one", archived=True)
    rc = verify.main(["archived-one"])
    assert rc == 2  # distinct from not_found's exit code 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "archived"


def test_verify_noop_within_window(temp_db, capsys):
    """Same-second double-verify must not churn FTS triggers."""
    mid = _seed_memory(temp_db, "double-verify")
    verify.main(["double-verify"])
    capsys.readouterr()  # drain first call

    rc = verify.main(["double-verify"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop"
    assert payload["reason"] == "recently_verified"
    assert payload["previous_last_verified_ts"] is not None
    _ = mid  # silence unused


def test_verify_updates_when_outside_window(temp_db, capsys):
    """If last_verified_ts is older than NOOP_WINDOW_SECONDS, restamp."""
    mid = _seed_memory(temp_db, "old-verify")
    # Force last_verified_ts to a long-ago timestamp.
    temp_db.execute(
        "UPDATE memories SET last_verified_ts = ? WHERE id = ?", (1000, mid)
    )

    rc = verify.main(["old-verify"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "verified"
    assert payload["previous_last_verified_ts"] == 1000

    row = temp_db.execute(
        "SELECT last_verified_ts FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    assert row["last_verified_ts"] > 1000
