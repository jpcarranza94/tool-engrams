"""Consolidation agent prompt — resolves via prompts/loader.py.

Lookup order:
    1. $ENGRAM_CONSOLIDATION_PROMPT_PATH
    2. ~/.claude/tool-engrams/prompts/consolidation.md
    3. toolengrams/prompts/defaults/consolidation.md

Variables interpolated: {target_date}, {session_list}, {memory_summary}.
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
