"""Detect secrets and sensitive data in memory bodies.

Gate for remember.py — rejects memories that contain API keys, passwords,
tokens, connection strings, or other credentials that should never be
persisted in the memory store.
"""

from __future__ import annotations

import re

# Known secret prefixes (case-sensitive where noted).
_SECRET_PREFIXES = [
    "sk-",           # OpenAI, Stripe secret keys
    "sk_live_",      # Stripe live
    "sk_test_",      # Stripe test
    "pk_live_",      # Stripe public live
    "pk_test_",      # Stripe public test
    "AKIA",          # AWS access key ID
    "ghp_",          # GitHub personal access token
    "gho_",          # GitHub OAuth token
    "ghs_",          # GitHub server-to-server token
    "ghu_",          # GitHub user-to-server token
    "github_pat_",   # GitHub fine-grained PAT
    "xoxb-",         # Slack bot token
    "xoxp-",         # Slack user token
    "xoxs-",         # Slack session token
    "xoxa-",         # Slack app token
    "Bearer ",       # Authorization header
    "Basic ",        # Basic auth header
    "eyJ",           # JWT (base64-encoded JSON)
    "ssh-rsa ",      # SSH public key (less sensitive but still)
    "ssh-ed25519 ",  # SSH public key
]

# Patterns that look like credential assignments.
_ASSIGNMENT_RE = re.compile(
    r"(?:password|passwd|secret|token|api_key|apikey|api[-_]?secret|"
    r"access_key|private_key|auth_token|client_secret|database_url|"
    r"db_password|db_pass|smtp_password|redis_url|mongo_uri|"
    r"encryption_key|signing_key|webhook_secret)"
    r"\s*[=:]\s*['\"]?.{8,}",
    re.IGNORECASE,
)

# Connection strings with embedded credentials.
_CONN_STRING_RE = re.compile(
    r"(?:postgresql|postgres|mysql|mongodb|redis|amqp|smtp)"
    r"(?:\+\w+)?://\w+:[^@\s]+@",
    re.IGNORECASE,
)

# Private key blocks.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\sKEY-----",
)

# High-entropy strings that look like API keys (32+ hex or base64 chars).
_HIGH_ENTROPY_RE = re.compile(
    r"(?<![a-zA-Z0-9/+])[A-Za-z0-9/+]{40,}(?:={0,2})(?![a-zA-Z0-9/+])",
)

# AWS secret access key pattern (40 char base64).
_AWS_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])",
)


def scan_for_secrets(text: str) -> list[str]:
    """Scan text for potential secrets. Returns list of finding descriptions.

    Empty list means no secrets detected.
    """
    findings: list[str] = []

    for prefix in _SECRET_PREFIXES:
        if prefix in text:
            findings.append(f"secret prefix: {prefix}...")
            break  # one prefix match is enough

    if _ASSIGNMENT_RE.search(text):
        findings.append("credential assignment (password=, token=, etc.)")

    if _CONN_STRING_RE.search(text):
        findings.append("connection string with embedded credentials")

    if _PRIVATE_KEY_RE.search(text):
        findings.append("private key block")

    # Only flag high-entropy if it's not inside a backticked command
    # (long hashes in git commands are fine).
    stripped = re.sub(r"`[^`]+`", "", text)
    match = _HIGH_ENTROPY_RE.search(stripped)
    if match:
        candidate = match.group()
        # Skip if it looks like a file path, or is a repeated character
        # (e.g. "xxxx..." padding). Require at least 10 distinct chars.
        distinct = len(set(candidate))
        if not candidate.startswith("/") and distinct >= 10:
            findings.append("high-entropy string (possible API key)")

    return findings
