"""User config file: ``<engram home>/config.json`` — durable, editable settings.

A single JSON file holds the runtime-switchable knobs that otherwise live only
in scattered ``ENGRAM_*`` env vars: the active **engine**, **per-engine models**,
background **watcher tuning**, and **prompt** overrides. ``hydrate_env()``
projects the file into ``os.environ`` for any mapped var **not already set**, so
every existing ``os.environ.get(...)`` call site keeps working unchanged and the
precedence is:

    explicit env  >  config file  >  built-in default

The file is read **fail-open**: a missing or malformed config never breaks a
hook (hot path) or a background tick. Switching the engine is just editing
``engine`` here — or ``engram engine set <name>`` — with no reinstall; the next
detached tick (a fresh process) picks it up.

Deliberately **not** file-backed: internal per-process vars
(``ENGRAM_IN_WATCHER``, ``ENGRAM_RUN_ID``, ``ENGRAM_ALLOWED_VERBS``,
``ENGRAM_DISABLED`` — the pause flag has its own file) and the bootstrap paths
``ENGRAM_HOME`` / ``ENGRAM_DB`` (the config file lives *under* the home, so the
home cannot be configured by it). See docs/adr/0012.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import envvars, paths
from .engine import selection

# __main__ already imports the whole CLI graph (incl. engine) on every hook
# invocation, so importing selection here is free — and it lets `set_value`
# validate the engine value against the real registry, closing the
# `config set engine <bogus>` door that bypassed `engram engine set`.

# The single source of truth for what the file can hold, how each key maps to an
# env var, and how `config set` coerces a value. Per-engine model keys mirror
# the engine adapters' own env names (claude-code unprefixed, codex CODEX-
# prefixed) — test_config::test_spec_matches_engine_adapters guards the drift.
SPEC: list[tuple[str, str, type]] = [
    ("engine", "ENGRAM_ENGINE", str),
    ("engines.claude-code.watcher_model", "ENGRAM_WATCHER_MODEL", str),
    ("engines.claude-code.formation_model", "ENGRAM_FORMATION_MODEL", str),
    ("engines.claude-code.eval_model", "ENGRAM_EVAL_MODEL", str),
    ("engines.codex.watcher_model", "ENGRAM_CODEX_WATCHER_MODEL", str),
    ("engines.codex.formation_model", "ENGRAM_CODEX_FORMATION_MODEL", str),
    ("engines.codex.eval_model", "ENGRAM_CODEX_EVAL_MODEL", str),
    ("watcher.tick_coalesce_sec", "ENGRAM_TICK_COALESCE_SEC", int),
    ("watcher.idle_sweep_sec", "ENGRAM_IDLE_SWEEP_SEC", int),
    ("watcher.timeout", "ENGRAM_WATCHER_TIMEOUT", int),
    ("watcher.cleanup_ttl_sec", "ENGRAM_CLEANUP_TTL_SEC", int),
    ("watcher.max_memories_per_call", "ENGRAM_MAX_MEMORIES_PER_CALL", int),
    ("watcher.surface_notice", "ENGRAM_SURFACE_NOTICE", str),
    ("watcher.max_form_retries", envvars.MAX_FORM_RETRIES, int),
    ("gate.threshold", envvars.GATE_THRESHOLD, float),
    ("gate.warmup_n", envvars.GATE_WARMUP_N, int),
    ("formation.similarity_threshold", envvars.SIMILARITY_THRESHOLD, float),
    ("consolidation.catchup_lookback_days", envvars.CATCHUP_LOOKBACK_DAYS, int),
    ("consolidation.surfaces_ttl_days", envvars.SURFACES_TTL_DAYS, int),
    ("consolidation.watcher_runs_ttl_days", envvars.WATCHER_RUNS_TTL_DAYS, int),
    ("consolidation.max_sessions", envvars.CONSOLIDATION_MAX_SESSIONS, int),
    ("consolidation.timeout", envvars.CONSOLIDATION_TIMEOUT, int),
    ("prompts.watcher_path", "ENGRAM_WATCHER_PROMPT_PATH", str),
    ("prompts.eval_path", "ENGRAM_EVAL_PROMPT_PATH", str),
    ("prompts.consolidation_path", "ENGRAM_CONSOLIDATION_PROMPT_PATH", str),
]

_ENV_BY_KEY = {key: env for key, env, _ in SPEC}
_TYPE_BY_KEY = {key: typ for key, _, typ in SPEC}


def _validate_engine(value: str) -> None:
    if value not in selection.ENGINES:
        raise ValueError(f"unknown engine {value!r}; known: "
                         f"{', '.join(selection.ENGINES)}")


# Per-key value validators (beyond the SPEC type coercion). Keyed by dotted
# key; absent means "any value of the declared type is accepted".
_VALIDATORS = {"engine": _validate_engine}


def config_path() -> Path:
    """Resolved at call time — `engram_home()` honors $ENGRAM_HOME (tests)."""
    return paths.engram_home() / "config.json"


def known_keys() -> list[str]:
    """Every settable dotted key, in declaration order (for `config set`/show)."""
    return [key for key, _, _ in SPEC]


def env_for(dotted: str) -> str | None:
    """The env var a dotted key maps to, or None if the key is unknown."""
    return _ENV_BY_KEY.get(dotted)


def load() -> dict:
    """Parsed config dict, or {} when absent/malformed (fail-open — a broken
    file must never break a hook or a background tick)."""
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(dotted: str):
    """Read one dotted key from the file (env is not consulted). None if unset."""
    return _dig(load(), dotted)


def hydrate_env() -> None:
    """Project file values into os.environ for any mapped var not already set.

    Precedence: an explicit env var always wins (we never overwrite). Call this
    once per process before any `os.environ.get("ENGRAM_*")` consumer runs —
    EXCEPT the `config`/`engine` management commands, which must compare env
    against file and so must see an un-hydrated environment.
    """
    data = load()
    if not data:
        return
    for key, env, _ in SPEC:
        if env in os.environ:
            continue
        value = _dig(data, key)
        if value is None:
            continue
        os.environ[env] = str(value)


def set_value(dotted: str, raw: str):
    """Coerce `raw` to the key's type, write it into the file, return the stored
    value. Raises KeyError for an unknown key, ValueError for a bad int."""
    if dotted not in _TYPE_BY_KEY:
        raise KeyError(dotted)
    value = _coerce(raw, _TYPE_BY_KEY[dotted])
    validator = _VALIDATORS.get(dotted)
    if validator is not None:
        validator(value)  # ValueError bubbles to the caller (CLI → exit 2)
    data = load()
    _bury(data, dotted, value)
    _write(data)
    return value


def unset(dotted: str) -> bool:
    """Remove a key from the file. Returns True if it was present. Raises
    KeyError for an unknown key (so a typo can't masquerade as 'already unset')."""
    if dotted not in _TYPE_BY_KEY:
        raise KeyError(dotted)
    data = load()
    if not _remove(data, dotted):
        return False
    _write(data)
    return True


def effective() -> list[dict]:
    """One row per known key: file value, env override (if any), the effective
    value, and its source — for `engram config show`."""
    data = load()
    rows = []
    for key, env, _ in SPEC:
        file_val = _dig(data, key)
        env_val = os.environ.get(env)
        if env_val is not None:
            eff, source = env_val, "env"
        elif file_val is not None:
            eff, source = file_val, "file"
        else:
            eff, source = None, "default"
        rows.append({"key": key, "env": env, "file": file_val,
                     "env_value": env_val, "effective": eff, "source": source})
    return rows


# ---------- internals ----------


def _coerce(raw: str, typ: type):
    if typ is int:
        return int(raw)  # ValueError bubbles up to the caller
    if typ is float:
        return float(raw)
    return raw


def _dig(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _bury(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _remove(data: dict, dotted: str) -> bool:
    parts = dotted.split(".")
    stack = [data]
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(nxt, dict):
            return False
        stack.append(nxt)
        cur = nxt
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False
    del cur[parts[-1]]
    # Prune now-empty parent containers so the file doesn't accrete {} husks.
    for part, parent in zip(reversed(parts[:-1]), reversed(stack[:-1])):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            del parent[part]
        else:
            break
    return True


def _write(data: dict) -> None:
    # Atomic write: a reader (hydrate_env on the hot path, selection's flat-key
    # read) must never observe a half-written file. Write to a sibling temp,
    # then os.replace — atomic within the same directory/filesystem.
    # NOTE: this is not locked, so a concurrent writer (a `config set` racing
    # install.sh's config write) can still lose an update. That window is tiny
    # and human-driven; the torn-READ class is the one worth eliminating here.
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
