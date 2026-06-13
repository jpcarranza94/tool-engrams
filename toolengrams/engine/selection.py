"""Engine selection — which headless runner does the background work.

Precedence (first hit wins):
    1. per-call `override` argument (tests, future `--engine` flags)
    2. $ENGRAM_ENGINE
    3. the "engine" key in <engram home>/config.json (a durable choice that
       survives launchd/cron's minimal env; the installer learns to write it
       when the codex engine lands)
    4. claude-code

Unknown names warn on stderr and fall back to claude-code — background work
is fail-open like everything else; `engram doctor`'s engine check reports
the misconfiguration loudly (detached ticks swallow stderr).
"""

from __future__ import annotations

import json
import os
import sys

from .. import paths
from ..harness_names import CLAUDE_CODE
from . import claude_code, codex
from .interface import EngineAdapter

ENGINES = {
    claude_code.NAME: claude_code,
    codex.NAME: codex,
}


def configured_engine_name(override: str | None = None) -> str:
    """The name selection WANTS (before the unknown-name fallback) — doctor
    compares it against the registry to surface misconfiguration."""
    return override or os.environ.get("ENGRAM_ENGINE") or _config_engine() or CLAUDE_CODE


def get_engine(override: str | None = None) -> "EngineAdapter":
    name = configured_engine_name(override)
    engine = ENGINES.get(name)
    if engine is None:
        print(f"engram: unknown engine {name!r}; falling back to {CLAUDE_CODE}",
              file=sys.stderr)
        return ENGINES[CLAUDE_CODE]
    return engine


def _config_engine() -> str | None:
    cfg = paths.engram_home() / "config.json"
    try:
        value = json.loads(cfg.read_text()).get("engine")
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and value else None
