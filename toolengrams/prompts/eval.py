"""Evaluation-watcher prompt — resolves via prompts/loader.py.

Lookup order:
    1. $ENGRAM_EVAL_PROMPT_PATH
    2. <engram home>/prompts/eval.md
    3. toolengrams/prompts/defaults/eval.md
"""

from .loader import load_prompt


def build_eval_prompt() -> str:
    return load_prompt("eval")

