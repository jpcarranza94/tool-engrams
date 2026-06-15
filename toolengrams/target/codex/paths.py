"""Codex filesystem locations used by the target adapter."""

from __future__ import annotations

import os
from pathlib import Path


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def config_path() -> Path:
    return Path(os.environ.get("CODEX_CONFIG") or (codex_home() / "config.toml"))


def hooks_path() -> Path:
    return Path(os.environ.get("CODEX_HOOKS") or (codex_home() / "hooks.json"))


def sessions_dir() -> Path:
    return codex_home() / "sessions"
