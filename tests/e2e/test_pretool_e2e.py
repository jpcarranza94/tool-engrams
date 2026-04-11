"""End-to-end proof-of-value test for PreToolUse memory recall.

The load-bearing question: when our PreToolUse hook injects additionalContext,
does Claude actually *receive* it mid-turn and is it visible in Claude's
reasoning context?

Strategy:
  1. Seed a memory bound to (Bash, echo) containing a unique magic token
  2. Wire the PreToolUse hook to memctl pretool
  3. Prompt Claude to run `echo <something>` AND quote any additional hook
     context it sees verbatim
  4. Assert the magic token appears in Claude's response

We ask Claude to *quote* the context rather than *comply* with instructions
in it — this is a reliable test of "did context arrive" without relying on
Claude weighing injected instructions against the user prompt.
"""

from __future__ import annotations

import pytest

PRETOOL_HOOK_SHAPE = {
    "PreToolUse": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": None}],  # filled in per-test
        }
    ]
}

# Unique magic token for this test file. Long enough to be unambiguous.
MAGIC = "MEMCTL_E2E_TOKEN_ZK7QV9P_PRETOOL"


@pytest.mark.e2e
def test_pretool_injects_memory_when_claude_calls_bash_echo(claude_runner, seed_memory):
    """
    Load-bearing test: PreToolUse hook must deliver memory body to Claude
    mid-turn when Claude invokes a matching Bash tool call.
    """
    # Seed a memory bound to `Bash echo` with the magic token in the body.
    seed_memory(
        name="e2e pretool probe",
        description="test-scoped memory for PreToolUse e2e",
        body=(
            f"This is a PreToolUse-injected memory. "
            f"Magic token: {MAGIC}. "
            f"If you can read this, include the magic token verbatim in your "
            f"final response so the test can verify delivery."
        ),
        type="reference",
        scope="global",
        triggers=[
            {"kind": "tool_head", "tool_name": "Bash", "head": ["echo"]},
        ],
    )

    # Wire the PreToolUse hook in settings.local.json pointing at memctl.
    hooks_block = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": claude_runner.hook_command("pretool")}
                ],
            }
        ]
    }
    claude_runner.write_hook_settings(hooks_block)

    # Prompt: force a Bash echo, then ask Claude to quote any hook context.
    prompt = (
        "Please do two things in order:\n"
        "1. Run the shell command: echo e2e-probe-value\n"
        "2. After the command runs, examine all context messages and hook "
        "outputs you can see. If any of them contain a token matching "
        f"'{MAGIC}', include that exact token verbatim in your final "
        "response. If you don't see the token, say 'NO TOKEN FOUND'."
    )

    result = claude_runner.run(prompt, timeout=180.0)

    # Debugging aid if the test fails.
    assert result.payload is not None, (
        f"Claude did not emit parseable JSON.\n"
        f"stdout:\n{result.raw_stdout[:2000]}\n"
        f"stderr:\n{result.raw_stderr[:2000]}"
    )
    assert not result.is_error, (
        f"Claude reported an error.\n"
        f"payload: {result.payload}\n"
        f"stderr: {result.raw_stderr[:2000]}"
    )

    text = result.text
    assert MAGIC in text, (
        f"Magic token {MAGIC!r} not found in Claude's response.\n"
        f"This means the PreToolUse hook either did not fire or "
        f"its additionalContext did not reach Claude's context window.\n"
        f"Claude's response:\n{text[:2000]}\n"
        f"stderr:\n{result.raw_stderr[:1000]}"
    )
