"""Guard against SKILL.md / CLI argparse drift.

Each skill file documents `engram <subcommand>` invocations. Every `--flag`
shown in those invocations must exist in the subcommand's argparse parser,
so a skill-driven call never errors on an unknown flag.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from toolengrams.cli import forget, recall, remember

REPO_ROOT = Path(__file__).resolve().parent.parent

SKILL_PARSERS = {
    "remember": ("remember", remember._build_parser),
    "recall": ("recall", recall._build_parser),
    "forget": ("forget", forget._build_parser),
}

_CODE_BLOCK = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.DOTALL)


def _documented_flags(skill_md: str, subcommand: str) -> set[str]:
    """Flags used on `engram <subcommand>` command lines in fenced code blocks.

    Quoted arguments (memory bodies, trigger prefixes) are kept as single
    shlex tokens, so flags *inside* them don't count.
    """
    flags: set[str] = set()
    for block in _CODE_BLOCK.findall(skill_md):
        # Join backslash continuations into single logical lines.
        joined = re.sub(r"\\\n", " ", block)
        for line in joined.splitlines():
            line = line.strip()
            if not line.startswith(f"engram {subcommand}"):
                continue
            # Synopsis brackets ([--name ...]) aren't shell syntax; drop them.
            line = line.replace("[", " ").replace("]", " ")
            for token in shlex.split(line):
                if token.startswith("--"):
                    flags.add(token)
    return flags


@pytest.mark.parametrize("skill_name", sorted(SKILL_PARSERS))
def test_skill_flags_exist_in_parser(skill_name):
    subcommand, build_parser = SKILL_PARSERS[skill_name]
    skill_md = (REPO_ROOT / "skills" / skill_name / "SKILL.md").read_text()

    documented = _documented_flags(skill_md, subcommand)
    assert documented, f"{skill_name}/SKILL.md documents no engram {subcommand} flags"

    # _option_string_actions is argparse-private but stable; this guard checks
    # flag NAMES only — a valid flag with a bad value still passes.
    known = set(build_parser()._option_string_actions)
    unknown = documented - known
    assert not unknown, (
        f"{skill_name}/SKILL.md documents flags missing from "
        f"`engram {subcommand}`: {sorted(unknown)}"
    )
