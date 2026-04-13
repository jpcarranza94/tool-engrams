"""Unit tests for `engram recall` — browse and search the memory store."""

from __future__ import annotations

import json
import time

from toolengrams.commands import recall


def _seed(conn, name: str, body: str = "body", **kwargs):
    defaults = {
        "description": "",
        "type": "reference",
        "scope": "global",
        "project_slug": None,
        "created_ts": int(time.time()),
        "pinned": 0,
    }
    defaults.update(kwargs)
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, description, body, type, scope, project_slug, created_ts, pinned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, defaults["description"], body, defaults["type"],
         defaults["scope"], defaults["project_slug"],
         defaults["created_ts"], defaults["pinned"]),
    )
    mid = cur.lastrowid
    # Add a tool_head trigger for testing
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
        "VALUES (?, 'tool_head', 'Bash', 'git', 1)",
        (mid,),
    )
    return mid


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = recall.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else {}


# ---------- list all ----------


def test_list_all(temp_db, capsys):
    _seed(temp_db, "memory one")
    _seed(temp_db, "memory two")
    rc, payload = _run([], capsys)
    assert rc == 0
    assert payload["count"] == 2
    names = {m["name"] for m in payload["memories"]}
    assert "memory one" in names
    assert "memory two" in names


def test_list_excludes_archived(temp_db, capsys):
    _seed(temp_db, "active")
    mid = _seed(temp_db, "archived")
    temp_db.execute("UPDATE memories SET archived_ts = ? WHERE id = ?", (int(time.time()), mid))
    rc, payload = _run([], capsys)
    assert payload["count"] == 1
    assert payload["memories"][0]["name"] == "active"


def test_list_respects_limit(temp_db, capsys):
    for i in range(5):
        _seed(temp_db, f"memory {i}")
    rc, payload = _run(["--limit", "2"], capsys)
    assert payload["count"] == 2


# ---------- search ----------


def test_search_by_keyword(temp_db, capsys):
    _seed(temp_db, "git commit rule", body="use HEREDOC for git commit messages")
    _seed(temp_db, "psql local", body="psql connects to local postgres")
    rc, payload = _run(["git"], capsys)
    assert rc == 0
    assert payload["count"] >= 1
    names = {m["name"] for m in payload["memories"]}
    assert "git commit rule" in names


def test_search_no_results(temp_db, capsys):
    _seed(temp_db, "something", body="unrelated content")
    rc, payload = _run(["zyxwvutsrqp"], capsys)
    assert rc == 0
    assert payload["count"] == 0


# ---------- detail ----------


def test_detail_by_id(temp_db, capsys):
    mid = _seed(temp_db, "detail test", body="detailed body here")
    rc, payload = _run(["--id", str(mid)], capsys)
    assert rc == 0
    assert payload["memory"]["name"] == "detail test"
    assert payload["memory"]["body"] == "detailed body here"
    assert len(payload["triggers"]) >= 1
    assert payload["triggers"][0]["kind"] == "tool_head"


def test_detail_not_found(temp_db, capsys):
    rc, payload = _run(["--id", "999"], capsys)
    assert rc == 1
    assert payload["error"] == "not_found"


# ---------- stats ----------


def test_stats(temp_db, capsys):
    _seed(temp_db, "ref1", type="reference")
    _seed(temp_db, "fb1", type="feedback")
    _seed(temp_db, "pinned1", pinned=1)
    rc, payload = _run(["--stats"], capsys)
    assert rc == 0
    assert payload["total"] == 3
    assert payload["active"] == 3
    assert payload["pinned"] == 1
    assert payload["by_type"]["reference"] == 2  # ref1 + pinned1 (default type)
    assert payload["by_type"]["feedback"] == 1
    assert payload["triggers_by_kind"]["tool_head"] == 3
