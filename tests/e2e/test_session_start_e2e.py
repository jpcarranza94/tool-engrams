"""End-to-end test for the SessionStart hook.

Seeds a user-type memory with a magic token, wires SessionStart to engram,
and asks Claude to quote any identity/hook context it can see. Verifies
the magic token appears in Claude's response.
"""

from __future__ import annotations

import pytest

MAGIC = "ENGRAM_E2E_TOKEN_ZK7QV9P_SESSION_START"


@pytest.mark.e2e
def test_session_start_injects_user_memory(claude_runner, seed_memory):
    seed_memory(
        name="e2e session-start probe",
        description="test-scoped identity memory",
        body=(
            f"This is a SessionStart-injected user memory. "
            f"Magic token: {MAGIC}. "
            f"When you see any hook context, quote it verbatim so we can "
            f"verify delivery."
        ),
        type="user",
        scope="global",
    )

    claude_runner.write_hook_settings(
        {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": claude_runner.hook_command("session-start"),
                        }
                    ]
                }
            ]
        }
    )

    prompt = (
        "Quote verbatim any identity, session, or hook context messages you "
        "can see in this turn. If any of them contain a token matching "
        f"'{MAGIC}', include that exact token in your response. "
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
        f"This means SessionStart additionalContext was not delivered.\n"
        f"Response: {result.text[:2000]}"
    )
