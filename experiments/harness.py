"""Shared experiment harness for claude -p calls with isolated ToolEngrams DB.

Each experiment gets:
  - A temp project dir with git init
  - An isolated SQLite DB
  - All hooks wired via .claude/settings.local.json
  - JSONL capture for post-hoc analysis
  - Helper methods for DB queries and JSONL parsing
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = sys.executable
CLAUDE_BIN = shutil.which("claude")


@dataclass
class RunResult:
    prompt: str
    text: str
    payload: dict[str, Any] | None
    returncode: int
    elapsed_s: float
    stderr: str
    session_id: str | None = None


@dataclass
class Experiment:
    """Isolated experiment environment with hooks wired to a test DB."""

    name: str
    project_dir: Path
    db_path: Path
    results: list[RunResult] = field(default_factory=list)

    @classmethod
    def create(cls, name: str, base_dir: Path) -> "Experiment":
        project_dir = base_dir / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        db_path = base_dir / "engram.sqlite"

        exp = cls(name=name, project_dir=project_dir, db_path=db_path)
        exp._init_git()
        exp._write_settings()
        return exp

    def _init_git(self):
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(self.project_dir),
            check=True,
            capture_output=True,
        )

    def _write_settings(self):
        hooks = {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": self._hook_cmd("session-start"),
                    "timeout": 5000,
                }],
            }],
            "PreToolUse": [{
                "matcher": "Bash|Read|Edit|Write|Grep|Glob|WebFetch|NotebookEdit",
                "hooks": [{
                    "type": "command",
                    "command": self._hook_cmd("pretool"),
                    "timeout": 3000,
                }],
            }],
            "PostToolUse": [{
                "matcher": "Bash|Read|Edit|Write|Grep|Glob|WebFetch|NotebookEdit",
                "hooks": [{
                    "type": "command",
                    "command": self._hook_cmd("post-tool"),
                    "timeout": 3000,
                }],
            }],
        }
        settings = {
            "hooks": hooks,
            "permissions": {
                "allow": [
                    "Bash(engram *)", "Bash(echo *)", "Bash(git *)",
                    "Bash(docker *)", "Bash(ls *)", "Bash(cat *)",
                    "Bash(pip *)", "Bash(uv *)", "Bash(python *)",
                    "Read", "Edit", "Write", "Grep", "Glob",
                ]
            },
        }
        settings_dir = self.project_dir / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))

    def _hook_cmd(self, subcommand: str) -> str:
        return (
            f"PYTHONPATH={REPO_ROOT} "
            f"ENGRAM_DB={self.db_path} "
            f"{PYTHON_BIN} -m toolengrams {subcommand}"
        )

    def run(self, prompt: str, timeout: float = 180.0) -> RunResult:
        if not CLAUDE_BIN:
            raise RuntimeError("claude CLI not on PATH")

        env = os.environ.copy()
        env["ENGRAM_DB"] = str(self.db_path)
        env["PYTHONPATH"] = str(REPO_ROOT)

        t0 = time.monotonic()
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "json", prompt],
            cwd=str(self.project_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - t0

        payload = _parse_first_json(proc.stdout)
        session_id = _extract_session_id(proc.stderr) or payload.get("session_id") if payload else None

        result = RunResult(
            prompt=prompt,
            text=(payload or {}).get("result", ""),
            payload=payload,
            returncode=proc.returncode,
            elapsed_s=round(elapsed, 1),
            stderr=proc.stderr[:2000] if proc.stderr else "",
            session_id=session_id,
        )
        self.results.append(result)
        return result

    def seed_memory(self, name: str, body: str, type_: str = "feedback",
                    scope: str = "global", triggers: list[dict] | None = None):
        """Insert a memory directly into the DB."""
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # Ensure schema
        schema = (REPO_ROOT / "toolengrams" / "schema.sql").read_text()
        conn.executescript(schema)
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO memories (name, description, body, type, scope, project_slug, created_ts) "
            "VALUES (?, '', ?, ?, ?, NULL, ?)",
            (name, body, type_, scope, now),
        )
        mid = cur.lastrowid
        for t in triggers or []:
            if t["kind"] == "tool_head":
                conn.execute(
                    "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
                    "VALUES (?, 'tool_head', ?, ?, ?)",
                    (mid, t["tool_name"], " ".join(t["head"]), len(t["head"])),
                )
            elif t["kind"] == "path_glob":
                conn.execute(
                    "INSERT INTO triggers (memory_id, kind, path_pattern) "
                    "VALUES (?, 'path_glob', ?)",
                    (mid, t["path_pattern"]),
                )
        conn.commit()
        conn.close()
        return mid

    def query_db(self, sql: str, params=()) -> list[dict]:
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result

    def memories(self) -> list[dict]:
        return self.query_db(
            "SELECT id, name, body, type, scope, surface_count, useful_count, pinned "
            "FROM memories WHERE archived_ts IS NULL"
        )

    def triggers(self) -> list[dict]:
        return self.query_db(
            "SELECT t.memory_id, t.kind, t.tool_name, t.head_joined, t.path_pattern "
            "FROM triggers t JOIN memories m ON t.memory_id = m.id "
            "WHERE m.archived_ts IS NULL"
        )

    def surfaces(self) -> list[dict]:
        return self.query_db(
            "SELECT session_id, memory_id, hook, tool_use_id, surfaced_ts "
            "FROM session_surfaces ORDER BY surfaced_ts"
        )

    def print_db_state(self, label: str = "DB State"):
        mems = self.memories()
        trigs = self.triggers()
        surfs = self.surfaces()
        print(f"\n--- {label} ---")
        print(f"Memories ({len(mems)}):")
        for m in mems:
            print(f"  [{m['id']}] {m['name']} | type={m['type']} surfaces={m['surface_count']} useful={m['useful_count']}")
            print(f"       body: {m['body'][:150]}")
        print(f"Triggers ({len(trigs)}):")
        for t in trigs:
            print(f"  mid={t['memory_id']} {t['kind']}: tool={t['tool_name']} head={t['head_joined']} path={t['path_pattern']}")
        print(f"Surfaces ({len(surfs)}):")
        for s in surfs:
            print(f"  mid={s['memory_id']} hook={s['hook']} tool_use_id={s.get('tool_use_id', 'N/A')}")

    def find_jsonls(self) -> list[Path]:
        """Find JSONL files created by this experiment's claude sessions."""
        pattern = str(self.project_dir).replace("/", "-")
        if pattern.startswith("-"):
            pattern = pattern[1:]
        # Claude stores transcripts under ~/.claude/projects/<slugified-cwd>/
        base = Path.home() / ".claude" / "projects"
        results = []
        for d in base.iterdir():
            if d.is_dir() and "proj" in d.name:
                for f in d.glob("*.jsonl"):
                    results.append(f)
        return sorted(results, key=lambda p: p.stat().st_mtime, reverse=True)

    def print_result(self, result: RunResult, label: str = ""):
        print(f"\n{'=' * 60}")
        if label:
            print(f"  {label}")
            print(f"{'=' * 60}")
        print(f"Prompt: {result.prompt[:120]}...")
        print(f"Duration: {result.elapsed_s}s | Return code: {result.returncode}")
        print(f"\nClaude response:\n{result.text[:1500]}")
        if result.stderr and "error" in result.stderr.lower():
            print(f"\nStderr: {result.stderr[:500]}")


def analyze_jsonl(path: Path, show_thinking: bool = True, show_tools: bool = True):
    """Parse and display a JSONL transcript."""
    print(f"\n--- JSONL: {path.name} ---")
    with open(path) as f:
        for i, line in enumerate(f):
            obj = json.loads(line)
            msg = obj.get("message", {})
            content = msg.get("content", "")
            role = msg.get("role", "")

            if not isinstance(content, list):
                continue

            for c in content:
                ct = c.get("type", "")
                if ct == "thinking" and show_thinking:
                    text = c.get("thinking", "")
                    print(f"\n[Line {i}] THINKING:")
                    print(f"  {text[:1000]}")
                elif ct == "tool_use" and show_tools:
                    print(f"\n[Line {i}] TOOL: {c.get('name')}")
                    inp = c.get("input", {})
                    print(f"  {json.dumps(inp)[:500]}")
                elif ct == "tool_result" and show_tools:
                    rc = c.get("content", "")
                    text = rc if isinstance(rc, str) else str(rc)
                    print(f"\n[Line {i}] RESULT:")
                    print(f"  {text[:500]}")
                elif ct == "text" and role == "assistant":
                    print(f"\n[Line {i}] RESPONSE:")
                    print(f"  {c.get('text', '')[:500]}")


def _parse_first_json(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _extract_session_id(stderr: str) -> str | None:
    for line in stderr.splitlines():
        if "session" in line.lower() and "id" in line.lower():
            parts = line.split()
            for p in parts:
                if len(p) > 20 and "-" in p:
                    return p
    return None
