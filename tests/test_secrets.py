"""Tests for secrets detection gate."""

from __future__ import annotations

import json

import pytest

from toolengrams.cli import remember
from toolengrams.formation import scan_for_secrets


# ---------- scan_for_secrets ----------


def test_clean_body_passes():
    assert scan_for_secrets("Use `git push --force-with-lease` instead of --force") == []


def test_clean_body_with_backticked_hash():
    """Long hashes inside backticks (git SHAs) should not trigger."""
    assert scan_for_secrets("Revert with `git revert abc123def456abc123def456abc123def456abc123`") == []


def test_detects_aws_access_key():
    findings = scan_for_secrets("Use `aws s3 ls` with key AKIAIOSFODNN7EXAMPLE")
    assert any("secret prefix" in f for f in findings)


def test_detects_openai_key():
    findings = scan_for_secrets("Set sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx as the key")
    assert any("secret prefix" in f for f in findings)


def test_detects_github_pat():
    findings = scan_for_secrets("Use ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx for auth")
    assert any("secret prefix" in f for f in findings)


def test_detects_slack_token():
    findings = scan_for_secrets("Bot token is xoxb-1234-5678-abcdefghijklmnop")
    assert any("secret prefix" in f for f in findings)


def test_detects_password_assignment():
    findings = scan_for_secrets("Connect with password=SuperSecret123!")
    assert any("credential assignment" in f for f in findings)


def test_detects_token_assignment():
    findings = scan_for_secrets('export API_KEY="long-secret-value-here-1234"')
    assert any("credential assignment" in f for f in findings)


def test_detects_connection_string():
    findings = scan_for_secrets("Use postgresql://admin:s3cret@db.example.com:5432/mydb")
    assert any("connection string" in f for f in findings)


def test_detects_mongo_connection_string():
    findings = scan_for_secrets("mongodb://user:pass@cluster.mongodb.net/db")
    assert any("connection string" in f for f in findings)


def test_detects_redis_url():
    findings = scan_for_secrets("redis://default:mypassword@redis.example.com:6379")
    assert any("connection string" in f for f in findings)


def test_detects_private_key():
    findings = scan_for_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
    assert any("private key" in f for f in findings)


def test_detects_jwt():
    findings = scan_for_secrets("Use eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjg")
    assert any("secret prefix" in f for f in findings)


def test_bearer_token():
    findings = scan_for_secrets('Header: "Authorization: Bearer abc123longtoken456"')
    assert any("secret prefix" in f for f in findings)


# ---------- remember.py integration ----------


def test_remember_rejects_body_with_api_key(temp_db, monkeypatch, capsys):
    """Memory with an API key in the body should be rejected."""
    rc = remember.main([
        "Use `curl -H 'Authorization: Bearer sk-proj-abc123def456'` to call the API",
        "--trigger", "curl",
        "--type", "reference",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "contains_secrets"


def test_remember_rejects_connection_string(temp_db, monkeypatch, capsys):
    rc = remember.main([
        "Connect with `psql postgresql://admin:password123@db.prod.com:5432/app`",
        "--trigger", "psql",
        "--type", "reference",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "contains_secrets"


def test_remember_accepts_clean_body(temp_db, monkeypatch, capsys):
    """Clean body with no secrets should be accepted."""
    rc = remember.main([
        "Use `psql -h replica.internal` for read-only queries",
        "--trigger", "psql -h replica",
        "--type", "reference",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "inserted"


def test_remember_dry_run_skips_secret_check(temp_db, monkeypatch, capsys):
    """Dry-run should show what would happen even with secrets (for debugging)."""
    rc = remember.main([
        "--dry-run",
        "Use `curl` with key sk-proj-abc123def456ghi789",
        "--trigger", "curl",
        "--type", "reference",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "dry_run"
