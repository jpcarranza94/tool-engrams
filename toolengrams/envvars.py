"""Environment-variable NAMES for the config-backed tuning knobs.

Single source of truth shared by the config SPEC (config.py) and the call-time
resolvers that read these vars. Without it the literal string lives in two files
(the resolver and the SPEC); a rename in one silently breaks the mapping — the
resolver would just always fall to its default and the config key would do
nothing, with no test catching it.

Leaf module: no imports, so any layer (including the hot-path scoring module)
can reference it without an import cycle.
"""

from __future__ import annotations

# Surfacing gate (reinforcement/scoring.py).
GATE_THRESHOLD = "ENGRAM_GATE_THRESHOLD"
GATE_WARMUP_N = "ENGRAM_GATE_WARMUP_N"

# Formation near-duplicate gate (cli/remember.py).
SIMILARITY_THRESHOLD = "ENGRAM_SIMILARITY_THRESHOLD"

# Consolidation (cli/consolidate.py, consolidation/agent.py).
CATCHUP_LOOKBACK_DAYS = "ENGRAM_CATCHUP_LOOKBACK_DAYS"
SURFACES_TTL_DAYS = "ENGRAM_SURFACES_TTL_DAYS"
WATCHER_RUNS_TTL_DAYS = "ENGRAM_WATCHER_RUNS_TTL_DAYS"
CONSOLIDATION_MAX_SESSIONS = "ENGRAM_CONSOLIDATION_MAX_SESSIONS"
CONSOLIDATION_TIMEOUT = "ENGRAM_CONSOLIDATION_TIMEOUT"

# Watcher (watcher/tick.py).
MAX_FORM_RETRIES = "ENGRAM_MAX_FORM_RETRIES"
