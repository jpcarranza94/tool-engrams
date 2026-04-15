"""Extract lookup hints (head-token prefixes, paths) from a tool call payload.

Given `(tool_name, tool_input)`, produce:
  - head_prefixes: list of 1- or 2-token prefixes of the command, as tuples
  - paths:         list of paths referenced by the call (file_path, url host, or
                   tilde/absolute paths embedded in Bash commands)

The head-token extraction generates *every prefix* of the leading token sequence
(length 1 and 2 for now). This lets the downstream lookup find memories bound
to either `[git]` or `[git, push]` from a single call like `git push origin main`.

Prefix matching on the second token happens at the database layer via
`head_joined LIKE ?||'%'` — see rank.py.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from .models import ExtractedTriggerHint

# Known CLI first tokens we care about for head extraction.
# Anything not in this list still gets a single-token head, but the second-token
# layer is only meaningful for tools that follow the "verb subcommand" pattern.
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

    tokens = _tokenize_bash(command)
    if not tokens:
        return

    head1 = tokens[0]
    hint.head_prefixes.append((head1,))

    if head1 in _SUBCOMMAND_TOOLS and len(tokens) >= 2:
        head2 = tokens[1]
        hint.head_prefixes.append((head1, head2))

    # Also include the full command as a head so that longer stored
    # triggers (e.g. "git push --force") can prefix-match against it.
    if len(tokens) > 2:
        hint.head_prefixes.append(tuple(tokens))

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
    # Use the host + first path segment as a head prefix: ("api.github.com",)
    stripped = re.sub(r"^https?://", "", url)
    host = stripped.split("/", 1)[0]
    if host:
        hint.head_prefixes.append((host,))


def join_head(tokens: tuple[str, ...]) -> str:
    """Serialize a head prefix to the canonical space-joined form used in the DB."""
    return " ".join(tokens)
