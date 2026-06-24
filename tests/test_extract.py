"""Trigger extraction tests.

Covers the `(tool_name, tool_input) -> ExtractedTriggerHint` mapping for the
tools in the PreToolUse whitelist. Extraction returns the full tokenization
of the call (not prefix pairs) and relies on subsequence matching downstream.
"""

from __future__ import annotations

from toolengrams.retrieval.extract import (
    ACCESS_ANY,
    ACCESS_READ,
    ACCESS_WRITE,
    access_mode_for_tool,
    extract_hints,
)


def test_bash_tokenizes_command():
    hint = extract_hints("Bash", {"command": "mycli -c 'SELECT 1'"})
    assert hint.tokens == ["mycli", "-c", "SELECT 1"]


def test_bash_subcommand_full_tokens():
    hint = extract_hints("Bash", {"command": "git push origin main"})
    assert hint.tokens == ["git", "push", "origin", "main"]


def test_bash_extracts_tilde_path():
    hint = extract_hints("Bash", {"command": "cat ~/.claude/settings.json"})
    assert "~/.claude/settings.json" in hint.paths


def test_bash_extracts_absolute_path():
    hint = extract_hints("Bash", {"command": "cat /etc/hosts | grep 127"})
    assert "/etc/hosts" in hint.paths


def test_bash_malformed_quoting_still_tokenizes():
    # Unterminated quote — shlex would raise; we fall back to whitespace split.
    hint = extract_hints("Bash", {"command": "git commit -m \"oops"})
    assert hint.tokens[0] == "git"
    assert "commit" in hint.tokens


def test_bash_empty_command():
    hint = extract_hints("Bash", {"command": ""})
    assert hint.tokens == []
    assert hint.paths == []


def test_read_file_path():
    hint = extract_hints("Read", {"file_path": "/home/user/projects/foo/bar.py"})
    assert hint.paths == ["/home/user/projects/foo/bar.py"]
    assert hint.tokens == []


def test_edit_file_path():
    hint = extract_hints("Edit", {"file_path": "~/.claude/CLAUDE.md"})
    assert "~/.claude/CLAUDE.md" in hint.paths


def test_webfetch_host_and_path_as_tokens():
    hint = extract_hints("WebFetch", {"url": "https://api.github.com/repos/foo/bar"})
    assert hint.tokens == ["api.github.com", "repos", "foo", "bar"]


def test_webfetch_no_scheme():
    hint = extract_hints("WebFetch", {"url": "example.com/path"})
    assert hint.tokens[0] == "example.com"


def test_grep_path():
    hint = extract_hints("Grep", {"pattern": "TODO", "path": "src/"})
    assert "src/" in hint.paths


def test_glob_pattern_and_path():
    hint = extract_hints("Glob", {"pattern": "**/*.py", "path": "toolengrams/"})
    assert "**/*.py" in hint.paths
    assert "toolengrams/" in hint.paths


def test_unknown_tool_no_hints():
    hint = extract_hints("SendMessage", {"to": "foo", "message": "bar"})
    assert hint.tokens == []
    assert hint.paths == []


# ---------- access-mode classification (issue #63) ----------


def test_access_mode_read_tools():
    assert access_mode_for_tool("Read") == ACCESS_READ
    assert access_mode_for_tool("Grep") == ACCESS_READ
    assert access_mode_for_tool("Glob") == ACCESS_READ


def test_access_mode_write_tools():
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert access_mode_for_tool(tool) == ACCESS_WRITE


def test_access_mode_other_tools_are_any():
    # Bash / WebFetch can read or write — neither read- nor write-only.
    assert access_mode_for_tool("Bash") == ACCESS_ANY
    assert access_mode_for_tool("WebFetch") == ACCESS_ANY
    assert access_mode_for_tool("SendMessage") == ACCESS_ANY
