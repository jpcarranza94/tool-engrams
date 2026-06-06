"""Evaluation-watcher prompt — resolves via prompts/loader.py.

Lookup order:
    1. $ENGRAM_EVAL_PROMPT_PATH
    2. ~/.claude/tool-engrams/prompts/eval.md
    3. toolengrams/prompts/defaults/eval.md
"""

from .loader import load_prompt


def build_eval_prompt() -> str:
    return load_prompt("eval")


# Header prepended to the delta on a resumed eval session (the standing guidance
# already lives in the session history; each pass just sends new evidence + the
# current pending list).
EVAL_SUBSEQUENT_HEADER = "--- New forward activity ---\n\n"
