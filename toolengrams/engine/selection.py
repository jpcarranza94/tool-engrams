"""Engine selection — which headless runner does the background work.

Precedence (first hit wins):
    1. per-call `override` argument (tests, future `--engine` flags)
    2. $ENGRAM_ENGINE
    3. the "engine" key in <engram home>/config.json (written by install.sh
       --engine; durable where launchd/cron's minimal env loses the var)
    4. claude-code

Unknown names warn on stderr and fall back to claude-code — background work
is fail-open like everything else; `engram doctor` reports misconfiguration
loudly.
"""

from __future__ import annotations

import json
import os
import sys

from .. import paths
from ..harness_names import CLAUDE_CODE
from . import claude_code

ENGINES = {
    claude_code.NAME: claude_code,
}


def get_engine(override: str | None = None):
    name = override or os.environ.get("ENGRAM_ENGINE") or _config_engine() or CLAUDE_CODE
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
