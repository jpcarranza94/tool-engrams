"""Tests for compound-token splitting in Bash extraction.

Regression: the 2026-04-24 consolidation agent flagged that a memory with
trigger `[ssh, 35.165.82.51]` didn't fire on `ssh ubuntu@35.165.82.51 …`
because shlex tokenizes `ubuntu@35.165.82.51` as one atom. The extractor
now also emits `ubuntu` and `35.165.82.51` as standalone tokens so
subsequence match works.
"""

from __future__ import annotations

from toolengrams.retrieval.extract import _expand_compound_tokens, extract_hints
from toolengrams.retrieval.rank import is_subsequence


def test_at_split_exposes_user_and_host_separately():
    tokens = _expand_compound_tokens(["ssh", "ubuntu@35.165.82.51", "md5sum"])
    assert "ssh" in tokens
    assert "ubuntu@35.165.82.51" in tokens  # original preserved
    assert "ubuntu" in tokens
    assert "35.165.82.51" in tokens


def test_no_split_when_no_delimiters():
    tokens = _expand_compound_tokens(["git", "push", "--force"])
    # Should be passthrough — no compound tokens.
    assert tokens == ["git", "push", "--force"]


def test_dedupes_repeated_parts():
    tokens = _expand_compound_tokens(["ssh", "foo@bar", "foo@baz"])
    # `foo` appears in both; should only be added once.
    assert tokens.count("foo") == 1


def test_empty_list_passthrough():
    assert _expand_compound_tokens([]) == []


# ---------- end-to-end: the bug the audit found ----------


def test_ssh_user_at_host_matches_bare_host_trigger():
    """Regression: ssh ubuntu@IP should subseq-match a trigger [ssh, IP]."""
    hint = extract_hints("Bash", {"command": "ssh ubuntu@35.165.82.51 uptime"})
    trigger_tokens = ("ssh", "35.165.82.51")
    assert is_subsequence(trigger_tokens, tuple(hint.tokens))


def test_ssh_user_at_host_matches_user_then_host_trigger():
    """Also works for explicit [ssh, ubuntu, 35.165.82.51]."""
    hint = extract_hints("Bash", {"command": "ssh ubuntu@35.165.82.51 uptime"})
    trigger_tokens = ("ssh", "ubuntu", "35.165.82.51")
    assert is_subsequence(trigger_tokens, tuple(hint.tokens))


def test_ssh_user_at_host_still_matches_full_compound_trigger():
    """Back-compat: a memory authored with [ssh, ubuntu@35.165.82.51] still matches."""
    hint = extract_hints("Bash", {"command": "ssh ubuntu@35.165.82.51 uptime"})
    trigger_tokens = ("ssh", "ubuntu@35.165.82.51")
    assert is_subsequence(trigger_tokens, tuple(hint.tokens))


def test_git_push_unchanged_by_expansion():
    """Ordinary git commands are unaffected — no `@`, no expansion."""
    hint = extract_hints("Bash", {"command": "git push --force origin main"})
    assert hint.tokens == ["git", "push", "--force", "origin", "main"]
