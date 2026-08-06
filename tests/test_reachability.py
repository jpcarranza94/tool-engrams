"""Reachability replay: the production matcher decides, and the archive gate
never touches a safety control."""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import reachability
from toolengrams.target.interface import SessionFile

DAY = 86400


def _mem(conn, name, body, tokens, *, kind="hint", pinned=False, age_days=200,
         path_pattern=None):
    created = int(time.time()) - age_days * DAY
    mid = memory_store.insert_memory(
        conn, name=name, description=None, body=body, kind=kind, scope="global",
        project_slug=None, pinned=pinned, created_ts=created,
    )
    if tokens:
        memory_store.add_token_trigger(conn, mid, tokens)
    if path_pattern:
        memory_store.add_path_trigger(conn, mid, path_pattern)
    return mid


def _session(tmp_path, commands):
    path = tmp_path / "sess.jsonl"
    path.write_text("".join(
        json.dumps({"type": "assistant", "cwd": "/tmp/proj", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash",
                         "input": {"command": cmd}}]}}) + "\n"
        for cmd in commands))
    return [SessionFile(path=path, session_id="s", project_slug="p",
                        modified_ts=0.0, size_bytes=path.stat().st_size,
                        target="claude-code")]


def test_scan_uses_production_matcher(temp_db, tmp_path):
    hit = _mem(temp_db, "force-push", "b", ["git", "push", "--force"])
    miss = _mem(temp_db, "drain", "b", ["kubectl", "drain"])

    reachable, tokens_seen = reachability.scan(
        temp_db, _session(tmp_path, ["git push --force origin main"]))

    assert reachable == {hit} and miss not in reachable
    assert "kubectl" not in tokens_seen and "git" in tokens_seen


def test_archive_never_touches_blocks_or_pinned(temp_db, tmp_path):
    """Blocks are safety controls: they have never fired precisely BECAUSE the
    dangerous command never came up. Same for pinned, young, path-glob, and
    still-live-command memories — every one is a veto."""
    block = _mem(temp_db, "no-prod-drop", "b", ["dropdb", "prod"], kind="block")
    pinned = _mem(temp_db, "pinned-fact", "b", ["terraform", "taint"], pinned=True)
    young = _mem(temp_db, "young-fact", "b", ["helm", "rollback"], age_days=3)
    globbed = _mem(temp_db, "path-only", "b", None, path_pattern="**/serverless.yml")
    live_cmd = _mem(temp_db, "over-narrow", "b", ["git", "bisect", "--term-old"])
    dead = _mem(temp_db, "truly-dead", "b", ["obsoletecli", "sync"])

    sessions = _session(tmp_path, ["git push --force origin main"])
    reachable, tokens_seen = reachability.scan(temp_db, sessions)
    memories = memory_store.list_memories(temp_db)
    never = [m for m in memories if m.id not in reachable]
    assert {m.id for m in never} == {block, pinned, young, globbed, live_cmd, dead}

    candidates, spared = reachability.archivable(
        never, memory_store.triggers_by_memory(temp_db), tokens_seen,
        int(time.time()) - 30 * DAY)

    assert [m.id for m in candidates] == [dead]
    assert spared["block_safety_control"] == 1 and spared["pinned"] == 1
