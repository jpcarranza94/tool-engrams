"""Fixtures for end-to-end tests that invoke `claude -p`.

These tests are skipped by default. Opt-in with `pytest -m e2e`.

Each test gets:
  - an isolated SQLite DB (set via ENGRAM_DB in the spawned claude process)
  - a temp project directory containing `.claude/settings.local.json` with
    the hook(s) under test wired to `engram` (ToolEngrams' CLI)
  - a `run_claude` helper that spawns `claude -p --output-format json` in
    the temp project dir and returns the parsed result

Key contract found empirically:
  - Project hooks load from `.claude/settings.local.json`, NOT `.claude/settings.json`
  - `--settings <file>` does NOT wire hooks (only permissions/env). Use the
    settings.local.json route instead.
  - `claude -p` writes one JSON line to stdout followed by a "Shell cwd was
    reset" line. We parse only the first non-empty line.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLAUDE_BIN = shutil.which("claude")
PYTHON_BIN = sys.executable


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests if the claude CLI isn't installed."""
    if CLAUDE_BIN is not None:
        return
    skip_marker = pytest.mark.skip(reason="claude CLI not found on PATH")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_marker)


@dataclass
class ClaudeResult:
    """Parsed `claude -p --output-format json` output."""

    raw_stdout: str
    raw_stderr: str
    returncode: int
    duration_ms: float
    payload: dict[str, Any] | None

    @property
    def text(self) -> str:
        if self.payload is None:
            return ""
        return self.payload.get("result") or ""

    @property
    def is_error(self) -> bool:
        return self.returncode != 0 or bool(self.payload and self.payload.get("is_error"))


@dataclass
class ClaudeRunner:
    """Test-scoped helper for spawning claude with isolated hook wiring."""

    project_dir: Path
    db_path: Path
    settings_path: Path

    def write_hook_settings(self, hooks: dict[str, list[dict]]) -> None:
        """Write a `.claude/settings.local.json` with the given hooks block."""
        settings = {"hooks": hooks}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2))

    def hook_command(self, subcommand: str) -> str:
        """Build a shell command that runs engram with the test DB + PYTHONPATH."""
        return (
            f"PYTHONPATH={REPO_ROOT} "
            f"ENGRAM_DB={self.db_path} "
            f"{PYTHON_BIN} -m toolengrams {subcommand}"
        )

    def run(self, prompt: str, timeout: float = 120.0) -> ClaudeResult:
        """Spawn `claude -p` in the project dir, return parsed result."""
        if CLAUDE_BIN is None:
            pytest.skip("claude CLI not found")

        env = os.environ.copy()
        env["ENGRAM_DB"] = str(self.db_path)
        env["PYTHONPATH"] = str(REPO_ROOT)

        t0 = time.monotonic()
        proc = subprocess.run(
            [
                CLAUDE_BIN,
                "-p",
                "--output-format",
                "json",
                prompt,
            ],
            cwd=str(self.project_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration_ms = (time.monotonic() - t0) * 1000.0

        payload = _parse_first_json_line(proc.stdout)
        return ClaudeResult(
            raw_stdout=proc.stdout,
            raw_stderr=proc.stderr,
            returncode=proc.returncode,
            duration_ms=duration_ms,
            payload=payload,
        )


def _parse_first_json_line(stdout: str) -> dict[str, Any] | None:
    """Parse the first non-empty line that looks like JSON.

    claude -p emits the result as a single JSON object on the first line
    and may append `Shell cwd was reset to ...` as a trailing line.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


@pytest.fixture
def claude_runner(tmp_path, monkeypatch) -> ClaudeRunner:
    """Create an isolated claude test project with a fresh SQLite DB."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    db_path = tmp_path / "engram.sqlite"
    settings_path = project_dir / ".claude" / "settings.local.json"

    # Point the in-process toolengrams at the test DB too — lets the test itself
    # seed via toolengrams.commands.seed or direct SQL inserts.
    monkeypatch.setenv("ENGRAM_DB", str(db_path))

    return ClaudeRunner(
        project_dir=project_dir,
        db_path=db_path,
        settings_path=settings_path,
    )


@pytest.fixture
def seed_memory(claude_runner):
    """Helper fixture: insert a single memory + triggers into the test DB.

    Usage:
        seed_memory(
            name="psql replica is read-only",
            body="Do not INSERT into the replica",
            type="reference",
            scope="global",
            triggers=[{"kind": "tool_head", "tool_name": "Bash", "head": ["psql", "-h"]}],
        )
    """
    from toolengrams import db

    def _insert(
        *,
        name: str,
        body: str,
        type: str = "reference",
        scope: str = "global",
        description: str = "",
        triggers: list[dict] | None = None,
    ) -> int:
        conn = db.connect(claude_runner.db_path)
        now_ts = int(time.time())
        cur = conn.execute(
            "INSERT INTO memories "
            "(name, description, body, type, scope, project_slug, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, description, body, type, scope, None, now_ts),
        )
        memory_id = cur.lastrowid
        for t in triggers or []:
            _insert_trigger(conn, memory_id, t)
        conn.close()
        return memory_id

    return _insert


@pytest.fixture
def db_assertions(claude_runner):
    """Deterministic assertions against the test DB state.

    Use this to verify hook pipeline behavior without relying on Claude's
    response (which may be flaky due to injection-defense heuristics).
    """
    from toolengrams import db

    class Assertions:
        def surfaces_for_session(self, hook: str | None = None) -> list[dict]:
            conn = db.connect(claude_runner.db_path)
            try:
                if hook:
                    rows = conn.execute(
                        "SELECT session_id, memory_id, hook FROM session_surfaces "
                        "WHERE hook = ?",
                        (hook,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT session_id, memory_id, hook FROM session_surfaces"
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        def memory_was_surfaced(self, memory_id: int, hook: str | None = None) -> bool:
            rows = self.surfaces_for_session(hook=hook)
            return any(r["memory_id"] == memory_id for r in rows)

        def surface_count(self, memory_id: int) -> int:
            conn = db.connect(claude_runner.db_path)
            try:
                row = conn.execute(
                    "SELECT surface_count FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                return row["surface_count"] if row else 0
            finally:
                conn.close()

    return Assertions()


def _insert_trigger(conn, memory_id: int, trigger: dict) -> None:
    import json as _json

    kind = trigger["kind"]
    # Back-compat shim: older e2e tests still use tool_head/head shape. Convert
    # on the fly into v2's token_subseq/tokens shape so the fixtures keep working.
    if kind == "tool_head":
        kind = "token_subseq"
        trigger = {"kind": kind, "tokens": list(trigger["head"])}

    if kind == "token_subseq":
        tokens = list(trigger["tokens"])
        if not tokens:
            return
        conn.execute(
            "INSERT INTO triggers "
            "(memory_id, kind, first_token, tokens_json) "
            "VALUES (?, 'token_subseq', ?, ?)",
            (memory_id, tokens[0], _json.dumps(tokens)),
        )
    elif kind == "path_glob":
        conn.execute(
            "INSERT INTO triggers (memory_id, kind, path_pattern) "
            "VALUES (?, 'path_glob', ?)",
            (memory_id, trigger["path_pattern"]),
        )
