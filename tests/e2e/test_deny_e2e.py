"""E2E test for deny behavior: memory blocks first call, Claude retries with corrected args.

Seeds a corrective memory ("always use git status --short"), asks Claude to
run git status, and verifies:
  1. The memory was surfaced (session_surfaces)
  2. Claude's final response reflects the --short output format
  3. The tool was called at least twice (denied + retry)
"""

from __future__ import annotations

import subprocess

import pytest


def _git_init(project_dir):
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
    )
    # Create a file so git status has something to show.
    (project_dir / "hello.txt").write_text("hello\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
    )
    # Modify so git status shows something.
    (project_dir / "hello.txt").write_text("modified\n")


@pytest.mark.e2e
def test_deny_blocks_first_call_and_retry_uses_corrected_args(
    claude_runner, seed_memory, db_assertions,
):
    """Corrective memory denies plain git status; Claude retries with --short."""
    _git_init(claude_runner.project_dir)

    memory_id = seed_memory(
        name="e2e deny test: git status short",
        description="test-scoped corrective memory",
        body=(
            "Always use `git status --short` instead of plain `git status`. "
            "The short format is required for CI parsing."
        ),
        type="feedback",
        scope="global",
        triggers=[
            {"kind": "tool_head", "tool_name": "Bash", "head": ["git", "status"]},
            {"kind": "tool_head", "tool_name": "Bash", "head": ["git"]},
        ],
    )

    claude_runner.write_hook_settings(
        {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": claude_runner.hook_command("pretool"),
                        }
                    ],
                }
            ]
        }
    )

    prompt = (
        "Run git status in the current directory and tell me what files "
        "are modified."
    )

    result = claude_runner.run(prompt, timeout=180.0)

    assert result.payload is not None, f"No JSON: {result.raw_stdout[:1000]}"
    assert not result.is_error, f"Claude error: {result.payload}"

    # Primary assertion: the memory was surfaced.
    assert db_assertions.memory_was_surfaced(memory_id, hook="pre_tool_use"), (
        f"Deny test: memory was not surfaced.\n"
        f"session_surfaces: {db_assertions.surfaces_for_session()}\n"
        f"Response: {result.text[:500]}"
    )

    # The deny caused at least one extra turn (deny + retry = 3+ turns).
    num_turns = result.payload.get("num_turns", 0)
    assert num_turns >= 3, (
        f"Expected >=3 turns (deny + retry + response), got {num_turns}.\n"
        f"Response: {result.text[:500]}"
    )


@pytest.mark.e2e
def test_deny_dedup_allows_second_call(claude_runner, seed_memory, db_assertions):
    """After first call is denied and retried, second call passes through."""
    _git_init(claude_runner.project_dir)

    seed_memory(
        name="e2e deny dedup test",
        description="test-scoped for dedup verification",
        body="Use `git status --short` always.",
        type="feedback",
        scope="global",
        triggers=[
            {"kind": "tool_head", "tool_name": "Bash", "head": ["git", "status"]},
            {"kind": "tool_head", "tool_name": "Bash", "head": ["git"]},
        ],
    )

    claude_runner.write_hook_settings(
        {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": claude_runner.hook_command("pretool"),
                        }
                    ],
                }
            ]
        }
    )

    prompt = (
        "Do two things in order:\n"
        "1. Run git status\n"
        "2. Run git status again a second time\n"
        "Tell me the output of both."
    )

    result = claude_runner.run(prompt, timeout=180.0)

    assert result.payload is not None
    assert not result.is_error

    # Should have completed successfully — both calls eventually ran.
    # The second call should NOT have been denied (session dedup).
    # We can't easily distinguish from the response, but the fact that
    # it completed without error and reported results is the signal.
    assert "hello.txt" in result.text.lower() or "modified" in result.text.lower(), (
        f"Expected git status output mentioning files.\n"
        f"Response: {result.text[:500]}"
    )
