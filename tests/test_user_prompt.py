"""Unit tests for the UserPromptSubmit handler."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.commands import user_prompt


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = user_prompt.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _insert_keyword_memory(conn, *, name, body, keyword):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, type, scope, created_ts) "
        "VALUES (?, '', ?, 'reference', 'global', ?)",
        (name, body, now),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, keyword) VALUES (?, 'keyword', ?)",
        (mid, keyword),
    )
    return mid


def _insert_path_glob_memory(conn, *, name, body, pattern):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, type, scope, created_ts) "
        "VALUES (?, '', ?, 'reference', 'global', ?)",
        (name, body, now),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) VALUES (?, 'path_glob', ?)",
        (mid, pattern),
    )
    return mid


def test_user_prompt_keyword_match(temp_db, monkeypatch):
    _insert_keyword_memory(
        temp_db, name="mycli note", body="mycli is read-only", keyword="mycli"
    )
    result = _run(
        {
            "session_id": "sp1",
            "cwd": "/tmp",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "can you check the db for recent records? use mycli",
        },
        monkeypatch,
    )
    assert "mycli is read-only" in result["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_path_glob_match(temp_db, monkeypatch):
    _insert_path_glob_memory(
        temp_db,
        name="settings.json hint",
        body="settings lives here",
        pattern="**/settings.json",
    )
    result = _run(
        {
            "session_id": "sp2",
            "cwd": "/tmp",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "please edit ~/.claude/settings.json to add a hook",
        },
        monkeypatch,
    )
    assert "settings lives here" in result["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_fts_match(temp_db, monkeypatch):
    now = int(time.time())
    temp_db.execute(
        "INSERT INTO memories (name, description, body, type, scope, created_ts) "
        "VALUES ('compliance note', '', 'auth middleware rewrite is compliance-driven, not tech-debt cleanup', 'project', 'global', ?)",
        (now,),
    )
    result = _run(
        {
            "session_id": "sp3",
            "cwd": "/tmp",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "what's the status of the auth middleware compliance project?",
        },
        monkeypatch,
    )
    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "compliance" in ctx


def test_user_prompt_empty_prompt_returns_empty(temp_db, monkeypatch):
    result = _run(
        {"session_id": "sp4", "cwd": "/tmp", "hook_event_name": "UserPromptSubmit", "prompt": ""},
        monkeypatch,
    )
    assert result == {}


def test_user_prompt_no_matches_returns_empty(temp_db, monkeypatch):
    _insert_keyword_memory(temp_db, name="k1", body="body", keyword="nevermentioned")
    result = _run(
        {
            "session_id": "sp5",
            "cwd": "/tmp",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "tell me about cheese",
        },
        monkeypatch,
    )
    assert result == {}
