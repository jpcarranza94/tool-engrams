"""Codex target adapter behavior."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from toolengrams.target import TARGETS, get_target
from toolengrams.target import codex

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex"


def test_codex_registered_and_contract_flags():
    assert get_target("codex") is codex
    assert TARGETS["codex"] is codex
    assert codex.tool_whitelist == frozenset({"Bash", "apply_patch"})
    assert codex.has_failure_event is False
    assert codex.min_version == "0.137.0"


def test_extract_hints_bash_reuses_neutral_command_extraction():
    hint = codex.extract_hints("Bash", {"command": "git push --force origin main"})
    assert hint.tool_name == "Bash"
    assert hint.tokens[:3] == ["git", "push", "--force"]


def test_extract_hints_apply_patch_paths_from_patch_envelope():
    patch = """*** Begin Patch
*** Add File: src/new.py
+print("hi")
*** Update File: pkg/existing.py
@@
-old
+new
*** Delete File: docs/old.md
*** End Patch
"""
    hint = codex.extract_hints("apply_patch", {"patch": patch})
    assert hint.tool_name == "apply_patch"
    assert hint.tokens == []
    assert hint.paths == ["src/new.py", "pkg/existing.py", "docs/old.md"]


def test_detect_failure_is_conservative():
    assert codex.detect_failure({"tool_response": {"ok": False}})
    assert codex.detect_failure({"tool_response": {"exit_code": 2}})
    assert codex.detect_failure({"tool_response": {"success": False}})
    assert codex.detect_failure({
        "tool_response": "ls: /no-such-dir: No such file or directory\n",
    })
    assert codex.detect_failure({
        "tool_response": "bash: mycli: command not found\n",
    })
    assert codex.detect_failure({
        "tool_response": "apply_patch verification failed: Failed to read file\n",
    })
    assert codex.detect_failure({"tool_response": "Process exited with code 1"})
    assert not codex.detect_failure({"tool_response": {"ok": True}})
    assert not codex.detect_failure({"tool_response": "Process exited with code 0"})
    assert not codex.detect_failure({"tool_response": "Exit code: 0\nOutput:\nok"})
    assert not codex.detect_failure({"tool_response": "plain output"})
    assert not codex.detect_failure({
        "tool_response": "scan complete: no such file or directory errors found\n",
    })


def test_transcript_path_payload_first_no_fallback():
    assert codex.transcript_path({"transcript_path": "/given.jsonl"}) == "/given.jsonl"
    assert codex.transcript_path({"session_id": "s", "cwd": "/tmp/project"}) == ""


def test_format_delta_emits_canonical_vocabulary_from_rollout_fixture():
    lines = (FIXTURE_DIR / "rollout" / "sample.jsonl").read_text().splitlines()
    out = codex.format_delta(lines)
    assert 'USER: "Please build it."' in out
    assert "TOOL (Bash): ls /no-such-dir-xyz" in out
    assert "RESULT: Chunk ID: x" in out
    assert "Process exited with code 1" in out
    assert "TOOL (apply_patch): sample.txt" in out
    assert "RESULT: Exit code: 0" in out
    assert "TOOL (apply_patch): missing-for-toolengrams-capture-2.txt" in out
    assert "RESULT: apply_patch verification failed" in out
    assert 'AGENT: "finished"' in out
    assert "<environment_context>" not in out


def test_collect_sessions_scans_codex_day_directory(tmp_path, monkeypatch):
    root = tmp_path / ".codex" / "sessions"
    day_dir = root / "2026" / "06" / "11"
    day_dir.mkdir(parents=True)
    rollout = day_dir / "rollout-2026-06-11T20-18-21-019eb99f.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "sess-1", "cwd": "/tmp/my-project"},
    }) + "\n")
    ts = datetime(2026, 6, 11, 12, 0).timestamp()
    os.utime(rollout, (ts, ts))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    sessions = codex.collect_sessions(date(2026, 6, 11))

    assert len(sessions) == 1
    assert sessions[0].path == rollout
    assert sessions[0].session_id == "sess-1"
    assert sessions[0].project_slug == "-tmp-my-project"


def test_hook_status_uses_codex_home(tmp_path, monkeypatch):
    custom = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    (custom).mkdir(parents=True)
    (custom / "config.toml").write_text("[features]\nhooks = true\n")
    hooks = {
        event: [{"hooks": [{"type": "command", "command": marker}]}]
        for event, marker in codex.hook_markers().items()
    }
    (custom / "hooks.json").write_text(json.dumps({"hooks": hooks}))

    assert codex.is_wired()


def test_installed_version_parses_stderr(monkeypatch):
    class Proc:
        stdout = ""
        stderr = "codex 0.137.0"

    monkeypatch.setattr(codex.subprocess, "run", lambda *a, **k: Proc())

    assert codex.installed_version() == "0.137.0"
