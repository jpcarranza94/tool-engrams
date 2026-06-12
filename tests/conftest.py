"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the repo root importable without an install step.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from toolengrams import db  # noqa: E402
from toolengrams.engine import EngineResult  # noqa: E402
from toolengrams.engine import claude_code as engine_claude_code  # noqa: E402


def make_fake_engine(invoke_fn=None, available: bool = True):
    """A fake engine adapter for watcher/consolidation tests: always-available
    binary, REAL claude-code sandbox translation (so settings.local.json
    assertions stay honest), and a caller-supplied invoke(req)."""
    return SimpleNamespace(
        NAME="claude-code",
        is_available=lambda: available,
        resolve_model=engine_claude_code.resolve_model,
        prepare_sandbox=engine_claude_code.prepare_sandbox,
        invoke=invoke_fn or (lambda req: EngineResult(ok=True, engine="claude-code")),
    )


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point ENGRAM_DB at a tmp file and yield a fresh connection."""
    path = tmp_path / "test.sqlite"
    monkeypatch.setenv("ENGRAM_DB", str(path))
    conn = db.connect(path)
    yield conn
    conn.close()
