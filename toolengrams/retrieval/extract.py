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

from ..models import AccessMode, ExtractedTriggerHint

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

# Read-vs-write access intent per file tool (issue #63). A path_glob trigger
# stores its own intent (triggers.access_mode); the call's intent — derived
# here from the tool name — is matched against it so an edit-intended memory
# stops firing on mere reads. Tools not in either set (Bash, WebFetch) are
# ACCESS_ANY: they can read or write, so they match path triggers of any mode.
ACCESS_READ: AccessMode = "read"
ACCESS_WRITE: AccessMode = "write"
ACCESS_ANY: AccessMode = "any"

_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Tools whose payload carries a single file_path/notebook_path, routed to
# _extract_from_file_tool. This is extraction-routing, NOT access
# classification: Grep/Glob are read tools but have their own extractors, and
# Read has no write semantics — so this set deliberately doesn't equal
# _READ_TOOLS ∪ _WRITE_TOOLS. Keeping it separate stops the two concerns from
# drifting when a future tool fits one grouping but not the other.
_FILE_PATH_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})


def access_mode_for_tool(tool_name: str) -> AccessMode:
    """The call's access intent: ACCESS_READ for read-only file tools,
    ACCESS_WRITE for mutating ones, ACCESS_ANY for everything else."""
    if tool_name in _READ_TOOLS:
        return ACCESS_READ
    if tool_name in _WRITE_TOOLS:
        return ACCESS_WRITE
    return ACCESS_ANY


def extract_hints(tool_name: str, tool_input: dict[str, Any]) -> ExtractedTriggerHint:
    hint = ExtractedTriggerHint(tool_name=tool_name)

    if tool_name == "Bash":
        _extract_from_bash(tool_input, hint)
    elif tool_name in _FILE_PATH_TOOLS:
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

    hint.tokens = _expand_compound_tokens(_tokenize_bash(command))

    for match in _PATH_RE.findall(command):
        if match not in hint.paths:
            hint.paths.append(match)


def _expand_compound_tokens(tokens: list[str]) -> list[str]:
    """Return `tokens` plus the parts of each compound token that humans
    typically treat as separate semantic atoms.

    Preserves order and original tokens; just adds peeled-off parts inline
    so subsequence matching can hit either the original or the part.

    Three compound shapes are unpacked:

    1. ``--flag=value`` → also yields ``--flag`` and ``value`` separately.
       This is the headline fix: triggers like ``["aws","logs","tail","--start-time"]``
       used to never match real calls because shlex keeps
       ``--start-time=2026-01-01`` as one token.

    2. URLs (``http://...``, ``https://...``) → also yields the host
       and optionally the first path segment. Triggers like
       ``["curl","jenkins.example.com"]`` used to never match real calls
       because shlex keeps the entire ``https://jenkins.example.com/api/v1``
       as one token.

    3. ``user@host`` → also yields ``user`` and ``host`` separately.
       Pre-existing behavior; preserved.

    We don't split on ``/`` (paths go through path_glob).
    """
    out: list[str] = []
    for tok in tokens:
        out.append(tok)
        for part in _compound_parts(tok):
            if part and part not in out:
                out.append(part)
    return out


def _compound_parts(tok: str) -> list[str]:
    """Extra atoms to surface alongside `tok` for subsequence matching."""
    # URL host (handle before flag/at — URL hosts can legitimately contain @/=).
    lower = tok.lower()
    if lower.startswith(("http://", "https://")):
        return _url_parts(tok)
    # Flag with value: --foo=bar → ["--foo", "bar"]; -X=Y likewise.
    if tok.startswith("-") and "=" in tok:
        flag, _, val = tok.partition("=")
        parts: list[str] = []
        if flag:
            parts.append(flag)
        if val:
            parts.append(val)
        return parts
    # user@host or ec2-user@1.2.3.4 → ["user", "host"]
    if "@" in tok:
        return [p for p in tok.split("@") if p]
    return []


def _url_parts(tok: str) -> list[str]:
    """Strip scheme, extract host + first path segment from a URL token."""
    after_scheme = tok.split("://", 1)[1] if "://" in tok else tok
    # Drop query / fragment before splitting on '/'.
    after_scheme = after_scheme.split("?", 1)[0].split("#", 1)[0]
    segments = [s for s in after_scheme.split("/") if s]
    if not segments:
        return []
    out = [segments[0]]  # host
    # First path segment too — lets triggers like ["curl", "session"] match
    # ``curl http://localhost:4096/session``.
    if len(segments) > 1:
        out.append(segments[1])
    return out


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
