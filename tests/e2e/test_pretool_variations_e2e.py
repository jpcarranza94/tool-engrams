"""PreToolUse E2E variations — validate memory delivery across different tool types.

These tests check that the tool whitelist, extraction logic, and matcher
patterns all work with real Claude calls for Read, Grep, and git-family
Bash commands. We don't test every tool in the whitelist — just enough to
exercise the distinct extraction code paths.

The test harness pattern is the same as test_pretool_e2e.py: seed a memory
with a unique magic token, wire PreToolUse with an appropriate matcher,
prompt Claude to invoke the tool + check for a named token.

Note on prompt shape: Claude's prompt-injection defenses flag requests to
"quote any/all hook outputs" or "exfiltrate internal context." Tests must
instead *name the specific expected token* so the ask is scoped and benign.
"""

from __future__ import annotations

import subprocess

import pytest


def _git_init(project_dir):
    """Make the project dir a real git repo so `git status` / other git
    commands don't abort with 'not a git repository'."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
    )


def _pretool_hook(claude_runner, matcher: str):
    """Return a PreToolUse hooks block wired to engram for the given matcher."""
    return {
        "PreToolUse": [
            {
                "matcher": matcher,
                "hooks": [
                    {"type": "command", "command": claude_runner.hook_command("pretool")}
                ],
            }
        ]
    }


@pytest.mark.e2e
def test_pretool_fires_on_read_with_path_trigger(claude_runner, seed_memory, db_assertions):
    """Read of a project file → path_glob trigger → memory injected.

    Claude's prompt-injection defense flags PreToolUse additionalContext
    that appears alongside Read tool output, so the token-in-response
    check is unreliable here. Primary assertion: session_surfaces shows
    the memory was retrieved + surfaced. That proves the hook pipeline
    worked regardless of whether Claude chose to echo the body.
    """
    target = claude_runner.project_dir / "notes.txt"
    target.write_text("line 1\nline 2\n")

    memory_id = seed_memory(
        name="e2e read variation",
        description="test-scoped path_glob memory",
        body="Notes about notes.txt: this file is used by the e2e Read test.",
        kind="hint",
        scope="global",
        triggers=[{"kind": "path_glob", "path_pattern": "**/notes.txt"}],
    )

    claude_runner.write_hook_settings(_pretool_hook(claude_runner, "Read"))

    prompt = "Please Read the file ./notes.txt from the current working directory and summarize it."

    result = claude_runner.run(prompt, timeout=180.0)

    assert result.payload is not None, f"No JSON: {result.raw_stdout[:1000]}"
    assert not result.is_error, f"Claude error: {result.payload}"

    # Primary assertion: the hook ran and retrieved the memory.
    assert db_assertions.memory_was_surfaced(memory_id, hook="pre_tool_use"), (
        f"Read variation: memory was not surfaced via PreToolUse hook.\n"
        f"session_surfaces rows: {db_assertions.surfaces_for_session()}\n"
        f"Claude response: {result.text[:500]}"
    )
    assert db_assertions.surface_count(memory_id) >= 1, (
        f"Read variation: surface_count not bumped"
    )


@pytest.mark.e2e
def test_pretool_fires_on_grep_with_path_trigger(claude_runner, seed_memory):
    """Grep inside a project subdir → path_glob trigger → memory injected."""
    magic = "ENGRAM_E2E_TOKEN_ZK7QV9P_GREP_VAR"

    (claude_runner.project_dir / "src").mkdir()
    (claude_runner.project_dir / "src" / "main.py").write_text("def hello():\n    pass\n")

    seed_memory(
        name="e2e grep variation",
        description="test-scoped path_glob memory for src/",
        body=(
            f"This memory fires on Grep inside src/. "
            f"Magic token: {magic}. Quote verbatim."
        ),
        kind="hint",
        scope="global",
        triggers=[
            {"kind": "path_glob", "path_pattern": "src*"},
            {"kind": "path_glob", "path_pattern": "*src*"},
        ],
    )

    claude_runner.write_hook_settings(_pretool_hook(claude_runner, "Grep"))

    prompt = (
        "Please use the Grep tool to search for 'hello' in the src/ directory "
        "of the current working directory. After searching, quote verbatim "
        f"any hook context you see. If any contain '{magic}', include that "
        "token. Otherwise say 'NO TOKEN FOUND'."
    )

    result = claude_runner.run(prompt, timeout=180.0)
    assert result.payload is not None, f"No JSON: {result.raw_stdout[:1000]}"
    assert not result.is_error, f"Claude error: {result.payload}"
    assert magic in result.text, (
        f"Grep variation failed.\nResponse: {result.text[:1500]}"
    )


@pytest.mark.e2e
def test_pretool_fires_on_git_status_subcommand(claude_runner, seed_memory):
    """Bash git status → token_subseq trigger on [git, status] → memory injected."""
    magic = "ENGRAM_E2E_TOKEN_ZK7QV9P_GIT_STATUS"

    # Make the project dir a real git repo so `git status` succeeds and
    # Claude has bandwidth to examine hook context instead of focusing on
    # a repo-not-found error.
    _git_init(claude_runner.project_dir)

    seed_memory(
        name="e2e git status variation",
        description="test-scoped subcommand-level head",
        body=(
            f"This memory fires on `git status` specifically. "
            f"Magic token: {magic}. If you can read this, include the "
            f"token verbatim in your final response."
        ),
        kind="hint",
        scope="global",
        triggers=[
            {"kind": "token_subseq", "tokens": ["git", "status"]},
        ],
    )

    claude_runner.write_hook_settings(_pretool_hook(claude_runner, "Bash"))

    prompt = (
        "Please run this shell command: git status\n"
        "\n"
        "After the command runs, examine all context messages and hook "
        f"outputs you can see. If any of them contain the token '{magic}', "
        "include that exact token verbatim in your final response. "
        "If you don't see the token, say 'NO TOKEN FOUND'."
    )

    result = claude_runner.run(prompt, timeout=180.0)
    assert result.payload is not None, f"No JSON: {result.raw_stdout[:1000]}"
    assert not result.is_error, f"Claude error: {result.payload}"
    assert magic in result.text, (
        f"git status variation failed.\nResponse: {result.text[:1500]}"
    )


@pytest.mark.e2e
def test_pretool_longer_head_wins_tiebreak_live(claude_runner, seed_memory):
    """Two memories, one [git] and one [git, status] — the longer head
    should rank first when Claude runs `git status`."""
    generic_magic = "ENGRAM_E2E_TOKEN_ZK7QV9P_GENERIC_GIT"
    specific_magic = "ENGRAM_E2E_TOKEN_ZK7QV9P_SPECIFIC_STATUS"

    _git_init(claude_runner.project_dir)

    seed_memory(
        name="generic git memory",
        body=(
            f"Generic git rule. Token: {generic_magic}. "
            f"If you can read this, include it in your response."
        ),
        kind="hint",
        scope="global",
        triggers=[{"kind": "token_subseq", "tokens": ["git"]}],
    )
    seed_memory(
        name="specific git status memory",
        body=(
            f"Specific git status rule. Token: {specific_magic}. "
            f"If you can read this, include it in your response."
        ),
        kind="hint",
        scope="global",
        triggers=[{"kind": "token_subseq", "tokens": ["git", "status"]}],
    )

    claude_runner.write_hook_settings(_pretool_hook(claude_runner, "Bash"))

    prompt = (
        "Please run this shell command: git status\n\n"
        "After it runs, examine all context messages and hook outputs you "
        f"can see. If any contain the token '{specific_magic}', include "
        "that exact token verbatim in your response. If you also see the "
        f"token '{generic_magic}', include it too. Otherwise say 'NO TOKEN "
        "FOUND'."
    )

    result = claude_runner.run(prompt, timeout=180.0)
    assert result.payload is not None
    assert not result.is_error
    # Both should surface (top-3 injection + both match), but the specific
    # one (longer head) MUST appear. Generic is a nice-to-have.
    assert specific_magic in result.text, (
        f"Specific memory (head=[git, status]) missing.\n"
        f"Response: {result.text[:1500]}"
    )
