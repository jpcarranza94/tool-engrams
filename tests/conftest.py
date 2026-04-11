"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable without an install step.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from toolengrams import db  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point ENGRAM_DB at a tmp file and yield a fresh connection."""
    path = tmp_path / "test.sqlite"
    monkeypatch.setenv("ENGRAM_DB", str(path))
    conn = db.connect(path)
    yield conn
    conn.close()
