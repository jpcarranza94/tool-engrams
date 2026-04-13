#!/usr/bin/env python3
"""Experiment: full memory lifecycle (formation → surfacing → reinforcement).

Runs two claude -p calls against an isolated DB:

1. FORMATION: Tell Claude a correction ("don't use docker compose without --build")
   and check if it runs `engram remember` to form a memory.

2. SURFACING: If formation succeeded, ask Claude to run `docker compose up`
   and check if the memory surfaces via PreToolUse.

Prints structured results and DB state after each step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = sys.executable
CLAUDE_BIN = shutil.which("claude")


def main():
    if not CLAUDE_BIN:
        print("ERROR: claude CLI not on PATH")
        return 1

    with tempfile.TemporaryDirectory(prefix="engram-experiment-") as tmpdir:
        tmpdir = Path(tmpdir)
        project_dir = tmpdir / "proj"
        project_dir.mkdir()
        db_path = tmpdir / "experiment.sqlite"

        runner = ExperimentRunner(project_dir, db_path)

        # Initialize git so Bash commands work
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(project_dir), check=True, capture_output=True)

        print("=" * 70)
        print("EXPERIMENT 1: MEMORY FORMATION")
        print("=" * 70)
        runner.run_formation_test()

        print("\n" + "=" * 70)
        print("EXPERIMENT 2: MEMORY SURFACING + REINFORCEMENT")
        print("=" * 70)
        runner.run_surfacing_test()

        print("\n" + "=" * 70)
        print("FINAL DB STATE")
        print("=" * 70)
        runner.dump_db_state()

    return 0


class ExperimentRunner:
    def __init__(self, project_dir: Path, db_path: Path):
        self.project_dir = project_dir
        self.db_path = db_path
        self._write_settings()

    def _write_settings(self):
        """Wire all hooks into settings.local.json."""
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
        settings_dir = self.project_dir / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)

        # Also add Bash(engram *) permission so formation can run
        settings = {
            "hooks": hooks,
            "permissions": {
                "allow": [
                    "Bash(engram *)",
                    "Bash(echo *)",
                    "Bash(git *)",
                    "Bash(docker *)",
                    "Bash(ls *)",
                    "Read",
                ]
            }
        }
        (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))

    def _hook_cmd(self, subcommand: str) -> str:
        return (
            f"PYTHONPATH={REPO_ROOT} "
            f"ENGRAM_DB={self.db_path} "
            f"{PYTHON_BIN} -m toolengrams {subcommand}"
        )

    def run_claude(self, prompt: str, timeout: float = 180.0) -> dict:
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

        # Parse first JSON line
        payload = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        return {
            "payload": payload,
            "text": (payload or {}).get("result", ""),
            "returncode": proc.returncode,
            "elapsed_s": round(elapsed, 1),
            "stderr": proc.stderr[:1000] if proc.stderr else "",
        }

    def query_db(self, sql: str, params=()) -> list[dict]:
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result

    def run_formation_test(self):
        prompt = (
            "I want to tell you something important for future reference: "
            "when running `docker compose up`, ALWAYS use the --build flag. "
            "Without it, Docker uses stale cached images and you'll waste time "
            "debugging phantom issues that are already fixed in the code. "
            "The correct command is `docker compose up --build`. "
            "Please save this as a memory using the engram remember command."
        )

        print(f"\nPrompt: {prompt[:100]}...")
        result = self.run_claude(prompt)
        print(f"Duration: {result['elapsed_s']}s")
        print(f"Return code: {result['returncode']}")
        print(f"\nClaude response:\n{result['text'][:1000]}")

        if result['stderr']:
            print(f"\nStderr (first 500):\n{result['stderr'][:500]}")

        # Check DB
        memories = self.query_db("SELECT id, name, body, type, scope FROM memories")
        triggers = self.query_db(
            "SELECT t.memory_id, t.kind, t.tool_name, t.head_joined, t.path_pattern "
            "FROM triggers t"
        )

        print(f"\n--- DB after formation ---")
        print(f"Memories: {len(memories)}")
        for m in memories:
            print(f"  [{m['id']}] {m['name']} (type={m['type']}, scope={m['scope']})")
            print(f"       body: {m['body'][:120]}...")
        print(f"Triggers: {len(triggers)}")
        for t in triggers:
            print(f"  memory_id={t['memory_id']} kind={t['kind']} "
                  f"tool={t.get('tool_name')} head={t.get('head_joined')} "
                  f"path={t.get('path_pattern')}")

    def run_surfacing_test(self):
        # Check if formation produced a memory
        memories = self.query_db("SELECT id FROM memories")
        if not memories:
            print("\nSKIPPED: No memories formed in experiment 1.")
            print("Seeding a memory manually for surfacing test...")
            self._seed_docker_memory()

        prompt = (
            "Please run this command: docker compose up\n"
            "After running it, tell me if you noticed any additional context "
            "or memories that were surfaced by hooks before the tool call."
        )

        print(f"\nPrompt: {prompt[:100]}...")
        result = self.run_claude(prompt)
        print(f"Duration: {result['elapsed_s']}s")
        print(f"Return code: {result['returncode']}")
        print(f"\nClaude response:\n{result['text'][:1000]}")

        # Check reinforcement
        memories = self.query_db(
            "SELECT id, name, surface_count, useful_count FROM memories"
        )
        surfaces = self.query_db(
            "SELECT session_id, memory_id, hook, tool_use_id FROM session_surfaces"
        )

        print(f"\n--- DB after surfacing ---")
        for m in memories:
            print(f"  [{m['id']}] {m['name']}: "
                  f"surface_count={m['surface_count']}, useful_count={m['useful_count']}")
        print(f"Session surfaces: {len(surfaces)}")
        for s in surfaces:
            print(f"  session={s['session_id'][:12]}... memory={s['memory_id']} "
                  f"hook={s['hook']} tool_use_id={s.get('tool_use_id', 'N/A')}")

    def _seed_docker_memory(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        # Ensure schema exists
        schema_path = REPO_ROOT / "toolengrams" / "schema.sql"
        conn.executescript(schema_path.read_text())
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO memories (name, description, body, type, scope, project_slug, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("docker compose always needs --build",
             "Avoid stale images",
             "When running `docker compose up`, ALWAYS use --build flag. "
             "Without it, Docker uses stale cached images.",
             "feedback", "global", None, now),
        )
        mid = cur.lastrowid
        conn.execute(
            "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
            "VALUES (?, 'tool_head', 'Bash', 'docker compose', 2)",
            (mid,),
        )
        conn.execute(
            "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
            "VALUES (?, 'tool_head', 'Bash', 'docker', 1)",
            (mid,),
        )
        conn.commit()
        conn.close()
        print("  Seeded docker compose memory with tool_head triggers.")

    def dump_db_state(self):
        memories = self.query_db(
            "SELECT id, name, type, scope, surface_count, useful_count, pinned FROM memories"
        )
        triggers = self.query_db(
            "SELECT memory_id, kind, tool_name, head_joined, path_pattern FROM triggers"
        )
        surfaces = self.query_db(
            "SELECT session_id, memory_id, hook, tool_use_id FROM session_surfaces"
        )
        print(f"\nMemories ({len(memories)}):")
        for m in memories:
            print(f"  [{m['id']}] {m['name']} | type={m['type']} scope={m['scope']} "
                  f"surfaces={m['surface_count']} useful={m['useful_count']} pinned={m['pinned']}")
        print(f"\nTriggers ({len(triggers)}):")
        for t in triggers:
            print(f"  mid={t['memory_id']} {t['kind']}: tool={t['tool_name']} "
                  f"head={t['head_joined']} path={t['path_pattern']}")
        print(f"\nSurfaces ({len(surfaces)}):")
        for s in surfaces:
            print(f"  session={s['session_id'][:16]}... mid={s['memory_id']} "
                  f"hook={s['hook']} tool_use_id={s.get('tool_use_id')}")


if __name__ == "__main__":
    raise SystemExit(main())
