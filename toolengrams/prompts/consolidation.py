"""Consolidation agent prompts — resolve via prompts/loader.py.

Lookup order (per prompt name):
    1. $ENGRAM_{NAME}_PROMPT_PATH
    2. <engram home>/prompts/{name}.md
    3. toolengrams/prompts/defaults/{name}.md

`consolidation` interpolates {target_date}, {session_list}, {memory_summary};
`consolidation_retry` interpolates {problems}.
"""

from .loader import load_prompt


def build_consolidation_prompt(
    session_list: str,
    memory_summary: str,
    target_date: str,
) -> str:
    return load_prompt(
        "consolidation",
        session_list=session_list,
        memory_summary=memory_summary,
        target_date=target_date,
    )


def build_consolidation_retry_prompt(problems: str) -> str:
    """Correction turn for a malformed report envelope, sent into the SAME
    consolidation session. `problems` is a human-readable summary of what failed
    validation (from report_parse.validate_envelope), quoted back to the agent.
    """
    return load_prompt("consolidation_retry", problems=problems)
