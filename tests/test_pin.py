"""Unit tests for `engram pin` — pin/unpin memories."""

from __future__ import annotations

import json
import time

from toolengrams.cli import pin


def _seed(conn, name: str = "test memory", pinned: int = 0):
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, description, body, kind, scope, project_slug, created_ts, pinned) "
        "VALUES (?, '', 'body', 'hint', 'global', NULL, ?, ?)",
        (name, int(time.time()), pinned),
    )
    return cur.lastrowid


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = pin.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else {}


def test_pin_memory(temp_db, capsys):
    mid = _seed(temp_db)
    rc, payload = _run(["test memory"], capsys)
    assert rc == 0
    assert payload["action"] == "pinned"
    assert payload["pinned"] is True

    row = temp_db.execute("SELECT pinned FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["pinned"] == 1


def test_unpin_memory(temp_db, capsys):
    mid = _seed(temp_db, pinned=1)
    rc, payload = _run(["--unpin", "test memory"], capsys)
    assert rc == 0
    assert payload["action"] == "unpinned"
    assert payload["pinned"] is False

    row = temp_db.execute("SELECT pinned FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["pinned"] == 0


def test_pin_fuzzy_match(temp_db, capsys):
    _seed(temp_db, name="psql replica is read-only")
    rc, payload = _run(["psql"], capsys)
    assert rc == 0
    assert "psql" in payload["name"]


def test_pin_not_found(temp_db, capsys):
    rc, payload = _run(["nonexistent"], capsys)
    assert rc == 1
    assert payload["error"] == "not_found"


def test_pin_no_args(temp_db, capsys):
    rc = pin.main([])
    assert rc == 2
