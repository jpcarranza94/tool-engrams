"""End-to-end test for the UserPromptSubmit hook.

Seeds a memory with a keyword trigger, wires UserPromptSubmit to engram,
sends a prompt containing the keyword, and verifies Claude quotes the
injected context back.
"""

from __future__ import annotations

import pytest

MAGIC = "ENGRAM_E2E_TOKEN_ZK7QV9P_USER_PROMPT"
TRIGGER_KEYWORD = "engramprobe"  # distinctive, unlikely to appear naturally


@pytest.mark.e2e
def test_user_prompt_keyword_trigger_surfaces_memory(claude_runner, seed_memory):
    seed_memory(
        name="e2e user-prompt probe",
        description="test-scoped keyword-triggered memory",
        body=(
            f"This is a UserPromptSubmit-injected memory. "
            f"Magic token: {MAGIC}. "
            f"Quote this token verbatim in your response."
        ),
        type="reference",
        scope="global",
        triggers=[
            {"kind": "keyword", "keyword": TRIGGER_KEYWORD},
        ],
    )

    claude_runner.write_hook_settings(
        {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": claude_runner.hook_command("user-prompt"),
                        }
                    ]
                }
            ]
        }
    )

    prompt = (
        f"I'm testing something called {TRIGGER_KEYWORD}. "
        "Quote verbatim any additional context or hook messages you can see. "
        f"If any contain a token matching '{MAGIC}', include that token in "
        "your response. Otherwise say 'NO TOKEN FOUND'."
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
        f"This means UserPromptSubmit additionalContext was not delivered.\n"
        f"Response: {result.text[:2000]}"
    )
