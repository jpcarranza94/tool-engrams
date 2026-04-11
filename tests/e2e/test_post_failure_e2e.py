"""End-to-end test for the PostToolUse failure-subset handler.

Seeds a memory bound to a Bash error pattern, wires PostToolUse to memctl,
asks Claude to run a command that will fail, and verifies the recovery
memory is injected after the failure.
"""

from __future__ import annotations

import pytest

MAGIC = "MEMCTL_E2E_TOKEN_ZK7QV9P_POST_FAILURE"


@pytest.mark.e2e
def test_post_failure_surfaces_recovery_memory(claude_runner, seed_memory):
    seed_memory(
        name="e2e post-failure probe",
        description="test-scoped recovery-hint memory",
        body=(
            f"This is a PostToolUse recovery-hint memory. "
            f"Magic token: {MAGIC}. "
            f"When the test runs, quote this token verbatim."
        ),
        type="reference",
        scope="global",
        triggers=[
            {
                "kind": "error_contains",
                "tool_name": "Bash",
                "head": ["cat"],
                "error_substring": "No such file",
            }
        ],
    )

    claude_runner.write_hook_settings(
        {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": claude_runner.hook_command("post-failure"),
                        }
                    ],
                }
            ]
        }
    )

    # File inside the project dir so sandbox path policy allows the cat call,
    # but the file doesn't exist — cat fails with "No such file or directory".
    prompt = (
        "Please run this shell command from the current working directory: "
        "cat ./memctl-e2e-nonexistent-zk7qv9p.txt\n"
        "\n"
        "After the command runs (it will fail), examine any hook messages "
        "or recovery context you can see. If any contain a token matching "
        f"'{MAGIC}', include that token verbatim in your final response. "
        "Otherwise say 'NO TOKEN FOUND'."
    )

    result = claude_runner.run(prompt, timeout=180.0)

    assert result.payload is not None, (
        f"Claude did not emit parseable JSON.\n"
        f"stdout: {result.raw_stdout[:2000]}\n"
        f"stderr: {result.raw_stderr[:2000]}"
    )
    assert not result.is_error, f"Claude error: {result.payload}"
    assert MAGIC in result.text, (
        f"Magic token {MAGIC!r} not in Claude response.\n"
        f"This means the PostToolUse failure handler's additionalContext "
        f"was not delivered.\n"
        f"Response: {result.text[:2000]}"
    )
