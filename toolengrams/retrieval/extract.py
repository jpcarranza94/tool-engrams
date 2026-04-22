"""Extract lookup hints (tokens, paths) from a tool call payload.

Given `(tool_name, tool_input)`, produce:
  - tokens: list of tokens representing the call
    - Bash: shell tokens (shlex-split the command)
    - WebFetch: URL host + path segments
  - paths: list of paths referenced by the call (file_path, absolute / tilde
    paths embedded in Bash, Grep/Glob paths, etc.)

The first token anchors the indexed lookup (`triggers.first_token`); the full
token list is subsequence-matched against stored trigger tokens at retrieval
time. See retrieval/rank.py for the matcher.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from ..models import ExtractedTriggerHint

# Known CLI first tokens we care about for second-token extraction during
# memory formation (see formation/candidates.py). Retrieval itself doesn't
# branch on this — it just subsequence-matches whatever tokens were stored.
_SUBCOMMAND_TOOLS = {
    "git", "gh", "jira", "docker", "aws", "kubectl", "bq", "psql",
    "npm", "yarn", "pnpm", "cargo", "pip", "brew", "make", "terraform",
    "ansible", "systemctl", "journalctl", "ssh", "scp", "rsync",
}

# Match ~/... or /abs/paths inside a Bash command string.
_PATH_RE = re.compile(r"(?<!\S)(~(?:/[^\s;|&><]*)?|/[^\s;|&><]+)")


def extract_hints(tool_name: str, tool_input: dict[str, Any]) -> ExtractedTriggerHint:
    hint = ExtractedTriggerHint(tool_name=tool_name)

    if tool_name == "Bash":
        _extract_from_bash(tool_input, hint)
    elif tool_name in {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}:
        _extract_from_file_tool(tool_input, hint)
    elif tool_name == "Grep":
        _extract_from_grep(tool_input, hint)
    elif tool_name == "Glob":
        _extract_from_glob(tool_input, hint)
    elif tool_name == "WebFetch":
        _extract_from_web(tool_input, hint)

    return hint


def _tokenize_bash(command: str) -> list[str]:
    """Shell-tokenize but tolerate malformed quoting."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _extract_from_bash(tool_input: dict[str, Any], hint: ExtractedTriggerHint) -> None:
    command = (tool_input.get("command") or "").strip()
    if not command:
        return

    hint.tokens = _tokenize_bash(command)

    for match in _PATH_RE.findall(command):
        if match not in hint.paths:
            hint.paths.append(match)


def _extract_from_file_tool(tool_input: dict[str, Any], hint: ExtractedTriggerHint) -> None:
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if path:
        hint.paths.append(str(path))


def _extract_from_grep(tool_input: dict[str, Any], hint: ExtractedTriggerHint) -> None:
    path = tool_input.get("path")
    if path:
        hint.paths.append(str(path))


def _extract_from_glob(tool_input: dict[str, Any], hint: ExtractedTriggerHint) -> None:
    pattern = tool_input.get("pattern")
    path = tool_input.get("path")
    if path:
        hint.paths.append(str(path))
    if pattern:
        hint.paths.append(str(pattern))


def _extract_from_web(tool_input: dict[str, Any], hint: ExtractedTriggerHint) -> None:
    url = tool_input.get("url")
    if not url:
        return
    stripped = re.sub(r"^https?://", "", url)
    parts = [p for p in stripped.split("/") if p]
    if parts:
        hint.tokens = parts
