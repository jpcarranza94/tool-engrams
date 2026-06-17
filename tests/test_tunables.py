"""Config-backed tuning knobs (the curated magic-numbers move).

Each knob keeps its module constant as the default and resolves the env var at
call time (after config.hydrate_env). These tests pin that the env override
actually reaches behavior — gate threshold, similarity threshold, catch-up
window — plus the env_int/env_float helpers. See docs/adr/0012.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

from toolengrams import config
from toolengrams.cli import consolidate
from toolengrams.reinforcement import scoring
from toolengrams.utils import env_float, env_int


# ---------- helpers ----------


def test_env_int_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("X_INT", "42")
    assert env_int("X_INT", 7) == 42
    monkeypatch.setenv("X_INT", "")
    assert env_int("X_INT", 7) == 7        # empty → default
    monkeypatch.setenv("X_INT", "nope")
    assert env_int("X_INT", 7) == 7        # unparseable → default
    monkeypatch.delenv("X_INT", raising=False)
    assert env_int("X_INT", 7) == 7        # unset → default


def test_env_float_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("X_F", "0.9")
    assert env_float("X_F", 0.5) == 0.9
    monkeypatch.setenv("X_F", "junk")
    assert env_float("X_F", 0.5) == 0.5


# ---------- gate threshold / warmup ----------


def _cand(useful, noise, kind="hint", pinned=False):
    return SimpleNamespace(useful_count=useful, noise_count=noise,
                           kind=kind, pinned=pinned)


def test_gate_threshold_env_override(monkeypatch):
    # q(2,1) = 3/5 = 0.6 → not gated at the 0.5 default...
    c = _cand(2, 1)
    monkeypatch.delenv("ENGRAM_GATE_THRESHOLD", raising=False)
    monkeypatch.delenv("ENGRAM_GATE_WARMUP_N", raising=False)
    assert scoring.is_gated(c) is False
    # ...but gated once the bar is raised above 0.6.
    monkeypatch.setenv("ENGRAM_GATE_THRESHOLD", "0.7")
    assert scoring.is_gated(c) is True


def test_gate_warmup_env_override(monkeypatch):
    c = _cand(0, 2)  # q low, but only 2 verdicts
    monkeypatch.setenv("ENGRAM_GATE_WARMUP_N", "5")
    assert scoring.is_gated(c) is False     # below warm-up → never gated
    monkeypatch.setenv("ENGRAM_GATE_WARMUP_N", "1")
    assert scoring.is_gated(c) is True


# ---------- catch-up lookback ----------


def test_catchup_lookback_env_override(monkeypatch):
    monkeypatch.setenv("ENGRAM_CATCHUP_LOOKBACK_DAYS", "3")
    dates = consolidate._resolve_dates(SimpleNamespace(date=None, yesterday=True))
    today = date.today()
    assert dates == [today - timedelta(days=n) for n in (3, 2, 1)]


# ---------- hydrate maps the new keys ----------


def test_new_tunable_keys_hydrate(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    for env in ("ENGRAM_GATE_THRESHOLD", "ENGRAM_SIMILARITY_THRESHOLD",
                "ENGRAM_CONSOLIDATION_MAX_SESSIONS"):
        monkeypatch.delenv(env, raising=False)
    config.set_value("gate.threshold", "0.42")
    config.set_value("formation.similarity_threshold", "0.8")
    config.set_value("consolidation.max_sessions", "20")

    config.hydrate_env()
    assert os.environ["ENGRAM_GATE_THRESHOLD"] == "0.42"
    assert os.environ["ENGRAM_SIMILARITY_THRESHOLD"] == "0.8"
    assert os.environ["ENGRAM_CONSOLIDATION_MAX_SESSIONS"] == "20"
