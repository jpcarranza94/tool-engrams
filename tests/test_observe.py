"""Tests for the observer's gating logic and judgment parsing."""

from __future__ import annotations

from toolengrams.observe import _SKIP_HEADS, _MIN_CMD_LENGTH, _try_save_from_judgment


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
    """Valid judgment with backticked command should create a memory."""
    _try_save_from_judgment(
        '{"name": "test-mem", "body": "Use `docker build --no-cache` to avoid stale layers.", '
        '"type": "feedback", "scope": "global"}'
    )
    rows = temp_db.execute("SELECT name, body FROM memories WHERE archived_ts IS NULL").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "test-mem"


def test_try_save_rejects_no_backticks(temp_db, capsys):
    """Judgment without backticked commands should be rejected."""
    _try_save_from_judgment(
        '{"name": "bad", "body": "No backticks here.", "type": "reference", "scope": "global"}'
    )
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_handles_markdown_wrapped_json(temp_db, capsys):
    """JSON wrapped in markdown fences should still parse."""
    _try_save_from_judgment(
        '```json\n{"name": "wrapped", "body": "Use `make test` before pushing.", '
        '"type": "feedback", "scope": "global"}\n```'
    )
    rows = temp_db.execute("SELECT name FROM memories WHERE archived_ts IS NULL").fetchall()
    assert len(rows) == 1


def test_try_save_handles_garbage(temp_db, capsys):
    """Unparseable response should not crash."""
    _try_save_from_judgment("this is not json at all")
    rows = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert rows["c"] == 0


def test_try_save_defaults_to_project_scope(temp_db, capsys):
    """Judgment without explicit scope should default to project."""
    _try_save_from_judgment(
        '{"name": "scoped-mem", "body": "Use `make deploy` in this repo.", '
        '"type": "reference", "triggers": ["make deploy"]}',
        cwd="/tmp/test-projects/myapp",
    )
    row = temp_db.execute(
        "SELECT scope, project_slug FROM memories WHERE name = 'scoped-mem'"
    ).fetchone()
    assert row is not None
    assert row["scope"] == "project"
    assert row["project_slug"] == "-tmp-test-projects-myapp"


def test_try_save_global_scope_has_no_slug(temp_db, capsys):
    """Explicit scope=global should not bind to a project."""
    _try_save_from_judgment(
        '{"name": "global-mem", "body": "Use `git push --force-with-lease`.", '
        '"type": "feedback", "scope": "global", "triggers": ["git push --force"]}',
        cwd="/tmp/test-projects/myapp",
    )
    row = temp_db.execute(
        "SELECT scope, project_slug FROM memories WHERE name = 'global-mem'"
    ).fetchone()
    assert row is not None
    assert row["scope"] == "global"
    assert row["project_slug"] is None
