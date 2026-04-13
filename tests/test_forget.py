"""Unit tests for `engram forget` — soft-demote, archive, topic demote, restore."""

from __future__ import annotations

import json
import time

import pytest

from toolengrams.commands import forget


def _seed(conn, name: str = "test memory", body: str = "test body", **kwargs):
    defaults = {
        "description": "",
        "type": "reference",
        "scope": "global",
        "project_slug": None,
        "created_ts": int(time.time()),
        "surface_count": 3,
        "useful_count": 2,
    }
    defaults.update(kwargs)
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, description, body, type, scope, project_slug, created_ts, surface_count, useful_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, defaults["description"], body, defaults["type"], defaults["scope"],
         defaults["project_slug"], defaults["created_ts"],
         defaults["surface_count"], defaults["useful_count"]),
    )
    return cur.lastrowid


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = forget.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else {}


# ---------- soft demote ----------


def test_soft_demote_by_exact_name(temp_db, capsys):
    mid = _seed(temp_db)
    rc, payload = _run(["test memory"], capsys)
    assert rc == 0
    assert payload["action"] == "soft_demoted"
    assert payload["memory_id"] == mid

    row = temp_db.execute("SELECT useful_count, surface_count, last_surfaced_ts FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["useful_count"] == 0
    assert row["surface_count"] == 8  # was 3, +5
    assert row["last_surfaced_ts"] == 0


def test_soft_demote_by_fuzzy_name(temp_db, capsys):
    _seed(temp_db, name="mycli is read-only prod replica")
    rc, payload = _run(["mycli"], capsys)
    assert rc == 0
    assert payload["action"] == "soft_demoted"
    assert "mycli" in payload["name"]


def test_not_found_returns_1(temp_db, capsys):
    rc, payload = _run(["nonexistent"], capsys)
    assert rc == 1
    assert payload["error"] == "not_found"


# ---------- hard delete ----------


def test_delete_sets_archived_ts(temp_db, capsys):
    mid = _seed(temp_db)
    rc, payload = _run(["--delete", "test memory"], capsys)
    assert rc == 0
    assert payload["action"] == "archived"

    row = temp_db.execute("SELECT archived_ts FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["archived_ts"] is not None


def test_archived_memory_not_found_by_default(temp_db, capsys):
    mid = _seed(temp_db)
    _run(["--delete", "test memory"], capsys)
    rc, payload = _run(["test memory"], capsys)
    assert rc == 1  # can't find it — it's archived


# ---------- topic demote ----------


def test_topic_demotes_all_matching(temp_db, capsys):
    _seed(temp_db, name="git commit rule", body="use HEREDOC for git commit")
    _seed(temp_db, name="git push rule", body="always push to feature branch with git")
    _seed(temp_db, name="unrelated", body="psql is local")

    rc, payload = _run(["--topic", "git"], capsys)
    assert rc == 0
    assert payload["count"] == 2
    names = {m["name"] for m in payload["memories"]}
    assert "git commit rule" in names
    assert "git push rule" in names
    assert "unrelated" not in names


def test_topic_no_matches(temp_db, capsys):
    _seed(temp_db, name="test", body="something")
    rc, payload = _run(["--topic", "nonexistentkeyword"], capsys)
    assert rc == 1


# ---------- restore ----------


def test_restore_undoes_archive(temp_db, capsys):
    mid = _seed(temp_db)
    _run(["--delete", "test memory"], capsys)

    rc, payload = _run(["--restore", "test memory"], capsys)
    assert rc == 0
    assert payload["action"] == "restored"

    row = temp_db.execute("SELECT archived_ts, surface_count, useful_count FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["archived_ts"] is None
    assert row["surface_count"] == 0
    assert row["useful_count"] == 0


def test_restore_undoes_soft_demote(temp_db, capsys):
    mid = _seed(temp_db)
    _run(["test memory"], capsys)  # soft demote

    rc, payload = _run(["--restore", "test memory"], capsys)
    assert rc == 0
    assert payload["action"] == "restored"

    row = temp_db.execute("SELECT surface_count, useful_count FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["surface_count"] == 0
    assert row["useful_count"] == 0


def test_restore_not_found(temp_db, capsys):
    rc, payload = _run(["--restore", "ghost"], capsys)
    assert rc == 1


# ---------- no args ----------


def test_no_args_returns_2(temp_db, capsys):
    rc = forget.main([])
    assert rc == 2
