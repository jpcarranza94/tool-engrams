"""Tests for the observer's gating logic and judgment parsing."""

from __future__ import annotations

from toolengrams.observe import (
    _SKIP_HEADS,
    _MIN_CMD_LENGTH,
    _FILE_TOOLS,
    _extract_signal,
    _try_save_from_judgment,
)
from toolengrams.transcript import is_sidechain_call


# ---------- gating ----------


def test_trivial_commands_are_skipped():
    for cmd in ["ls -la", "echo hello", "cat file.txt", "head -5 foo", "engram recall"]:
        first = cmd.split()[0]
        assert first in _SKIP_HEADS, f"{cmd} should be skipped"


def test_short_commands_below_threshold():
    assert len("git status") < _MIN_CMD_LENGTH


def test_nontrivial_commands_pass():
    cmds = [
        "git push origin main",
        "aws logs tail --follow /aws/ec2/prod",
        "docker compose up --build",
        "ssh user@host 'some command here'",
    ]
    for cmd in cmds:
        first = cmd.split()[0]
        assert first not in _SKIP_HEADS, f"{cmd} should not be skipped"
        assert len(cmd) >= _MIN_CMD_LENGTH, f"{cmd} should be long enough"


# ---------- judgment parsing ----------


def test_try_save_skip_judgment(temp_db, capsys):
    """Skip judgment should not create a memory."""
    _try_save_from_judgment('{"action": "skip", "reason": "trivial"}')
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_valid_judgment(temp_db, capsys):
    """Valid judgment with action=save and explicit trigger should create a memory."""
    _try_save_from_judgment(
        '{"action": "save", "name": "test-mem", "body": "Use `docker build --no-cache` to avoid stale layers.", '
        '"type": "feedback", "scope": "global", "triggers": ["docker build"]}'
    )
    rows = temp_db.execute("SELECT name, body FROM memories WHERE archived_ts IS NULL").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "test-mem"


def test_try_save_rejects_no_triggers_or_paths(temp_db, capsys):
    """Judgment with action=save but no triggers or paths should be rejected."""
    _try_save_from_judgment(
        '{"action": "save", "name": "bad", "body": "No triggers.", "type": "reference", "scope": "global"}'
    )
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_requires_action_save(temp_db, capsys):
    """Judgment with action != 'save' should not create a memory."""
    _try_save_from_judgment(
        '{"action": "skip"}'
    )
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0

    # Even with full fields, action must be "save"
    _try_save_from_judgment(
        '{"action": "skip", "name": "nope", "body": "Use `git`.", "triggers": ["git"]}'
    )
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_handles_garbage(temp_db, capsys):
    """Unparseable response should not crash."""
    _try_save_from_judgment("this is not json at all")
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_defaults_to_project_scope(temp_db, capsys):
    """Judgment without explicit scope should default to project."""
    _try_save_from_judgment(
        '{"action": "save", "name": "scoped-mem", "body": "Use `make deploy` in this repo.", '
        '"type": "reference", "triggers": ["make deploy"]}',
        cwd="/tmp/test-projects/myapp",
    )
    row = temp_db.execute(
        "SELECT scope, project_slug FROM memories WHERE name = 'scoped-mem'"
    ).fetchone()
    assert row is not None
    assert row["scope"] == "project"
    assert row["project_slug"] == "-tmp-test-projects-myapp"


# ---------- signal extraction ----------


def test_signal_bash_command():
    kind, value = _extract_signal("Bash", {"command": "git push origin main"})
    assert kind == "command"
    assert value == "git push origin main"


def test_signal_bash_short_command_skipped():
    kind, _ = _extract_signal("Bash", {"command": "ls"})
    assert kind is None


def test_signal_bash_trivial_head_skipped():
    kind, _ = _extract_signal("Bash", {"command": "echo hello world foo bar"})
    assert kind is None


def test_signal_edit_file_tool():
    kind, value = _extract_signal("Edit", {"file_path": "/repo/src/billing.py"})
    assert kind == "file"
    assert value == "/repo/src/billing.py"


def test_signal_write_file_tool():
    kind, value = _extract_signal("Write", {"file_path": "/repo/new.py"})
    assert kind == "file"


def test_signal_multiedit_tool():
    kind, _ = _extract_signal("MultiEdit", {"file_path": "/repo/a.py"})
    assert kind == "file"


def test_signal_read_tool_not_observed():
    """Read is too common to trigger the observer."""
    kind, _ = _extract_signal("Read", {"file_path": "/repo/a.py"})
    assert kind is None


def test_signal_noise_paths_skipped():
    kind, _ = _extract_signal("Edit", {"file_path": "/repo/node_modules/foo/x.js"})
    assert kind is None
    kind, _ = _extract_signal("Edit", {"file_path": "/repo/.venv/lib/x.py"})
    assert kind is None
    kind, _ = _extract_signal("Write", {"file_path": "/repo/__pycache__/x.pyc"})
    assert kind is None


# ---------- sidechain detection ----------


def test_sidechain_detection_normal_call():
    """Normal user-driven call has no agent_id/agent_type."""
    payload = {
        "session_id": "abc",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": "/tmp/project",
    }
    assert is_sidechain_call(payload) is False


def test_sidechain_detection_task_subagent():
    """Task-spawned subagent call has agent_id and agent_type."""
    payload = {
        "session_id": "abc",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "agent_id": "xyz-123",
        "agent_type": "general-purpose",
    }
    assert is_sidechain_call(payload) is True


def test_sidechain_detection_agent_type_only():
    """agent_type alone is enough to identify a sidechain."""
    payload = {"tool_name": "Bash", "agent_type": "Explore"}
    assert is_sidechain_call(payload) is True


def test_try_save_with_paths(temp_db, capsys):
    """Judgment with only paths (no triggers) should save a path-bound memory."""
    _try_save_from_judgment(
        '{"action": "save", "name": "billing-decimal", "body": "Files in billing/ must use custom Decimal precision.", '
        '"type": "feedback", "scope": "project", "paths": ["**/billing/*.py"]}',
        cwd="/tmp/test-projects/myapp",
    )
    row = temp_db.execute(
        "SELECT m.id, t.kind, t.path_pattern FROM memories m "
        "JOIN triggers t ON t.memory_id = m.id "
        "WHERE m.name = 'billing-decimal'"
    ).fetchone()
    assert row is not None
    assert row["kind"] == "path_glob"
    assert row["path_pattern"] == "**/billing/*.py"


def test_try_save_no_triggers_no_paths_skipped(temp_db, capsys):
    """Judgment without triggers or paths must not create a memory."""
    _try_save_from_judgment(
        '{"action": "save", "name": "empty", "body": "Use `git status`.", '
        '"type": "reference", "scope": "global"}'
    )
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_global_scope_has_no_slug(temp_db, capsys):
    """Explicit scope=global should not bind to a project."""
    _try_save_from_judgment(
        '{"action": "save", "name": "global-mem", "body": "Use `git push --force-with-lease`.", '
        '"type": "feedback", "scope": "global", "triggers": ["git push --force"]}',
        cwd="/tmp/test-projects/myapp",
    )
    row = temp_db.execute(
        "SELECT scope, project_slug FROM memories WHERE name = 'global-mem'"
    ).fetchone()
    assert row is not None
    assert row["scope"] == "global"
    assert row["project_slug"] is None
