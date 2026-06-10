"""_expand_compound_tokens: --flag=val splitting, URL host extraction,
user@host pre-existing behavior preserved."""

from __future__ import annotations

from toolengrams.retrieval.extract import _expand_compound_tokens, extract_hints


def test_flag_value_token_split():
    """--start-time=2026-01-01 yields ['--start-time', '2026-01-01'] inline."""
    out = _expand_compound_tokens(["aws", "logs", "tail", "--start-time=2026-01-01"])
    assert out[0] == "aws"
    assert "--start-time=2026-01-01" in out  # original preserved
    assert "--start-time" in out
    assert "2026-01-01" in out


def test_short_flag_with_value():
    out = _expand_compound_tokens(["foo", "-X=val"])
    assert "-X" in out
    assert "val" in out


def test_url_host_extraction():
    """https://host/path/v1 yields 'host' and the first path segment."""
    out = _expand_compound_tokens(["curl", "https://jenkins.example.com/api/v1"])
    assert "jenkins.example.com" in out
    assert "api" in out


def test_url_with_localhost_and_port():
    out = _expand_compound_tokens(["curl", "http://localhost:4096/session"])
    assert "localhost:4096" in out
    assert "session" in out


def test_url_strips_query_and_fragment():
    out = _expand_compound_tokens(["curl", "https://host.com/a?x=1#frag"])
    assert "host.com" in out
    assert "a" in out
    # Query/fragment shouldn't bleed into the path segment.
    assert "a?x=1" not in out


def test_user_at_host_preserved():
    """Existing @ split still works."""
    out = _expand_compound_tokens(["ssh", "ec2-user@1.2.3.4"])
    assert "ec2-user" in out
    assert "1.2.3.4" in out


def test_env_var_assignment_not_flag_split():
    """STAGING_FOO=bar is not a flag (doesn't start with -), don't split."""
    out = _expand_compound_tokens(["STAGING_FOO=bar", "cmd"])
    assert out == ["STAGING_FOO=bar", "cmd"]


def test_extract_hints_chains_url_expansion():
    """End-to-end: a curl call with a URL produces host+segment tokens."""
    h = extract_hints("Bash", {"command": "curl https://jenkins.example.com/api/v1"})
    assert "jenkins.example.com" in h.tokens
    assert "api" in h.tokens


def test_extract_hints_chains_flag_expansion():
    h = extract_hints("Bash", {"command": "aws logs tail --start-time=2026-01-01"})
    assert "--start-time" in h.tokens
    assert "2026-01-01" in h.tokens


def test_audit_failure_case_jira_flag():
    """The audit example: jira issue create --assignee=foo."""
    h = extract_hints("Bash", {"command": "jira issue create --assignee=foo"})
    assert "--assignee" in h.tokens
    assert "foo" in h.tokens
