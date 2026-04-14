"""Agent-based consolidation: spawn an Opus agent to review today's sessions.

Instead of a brittle pipeline (regex → truncated episodes → JSON prompt),
we give an Opus agent the raw session files, the engram CLI, and let it
explore freely. The agent reads transcripts, evaluates memory surfacing
quality, identifies missed corrections, and runs engram commands directly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..prompts.consolidation import build_consolidation_prompt
from .collect import SessionFile

CLAUDE_BIN = shutil.which("claude")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(slots=True)
class AgentResult:
    report: str
    returncode: int
    raw_stdout: str
    raw_stderr: str
    error: str | None = None


def run_consolidation_agent(
    sessions: list[SessionFile],
    db_path: Path,
    target_date: str,
) -> AgentResult:
    """Spawn an Opus agent to review today's sessions and consolidate memories."""
    if not CLAUDE_BIN:
        return AgentResult(
            report="", returncode=1, raw_stdout="", raw_stderr="",
            error="claude CLI not found on PATH",
        )

    if not sessions:
        return AgentResult(
            report="No sessions to review.", returncode=0,
            raw_stdout="", raw_stderr="",
        )

    # Build the agent's working environment.
    work_dir = tempfile.mkdtemp(prefix="engram-consolidate-")
    work_path = Path(work_dir)
    _write_agent_settings(work_path, db_path)

    # Build the prompt.
    memory_summary = _get_memory_summary(db_path)
    session_list = "\n".join(
        f"  {s.path} ({s.size_bytes / 1024:.0f} KB) — session {s.session_id[:12]}..."
        for s in sessions
    )
    prompt = build_consolidation_prompt(session_list, memory_summary, target_date)

    env = os.environ.copy()
    env["ENGRAM_DB"] = str(db_path)

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "json", prompt],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minutes max
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            report="", returncode=1, raw_stdout="", raw_stderr="",
            error="Consolidation agent timed out (30 min)",
        )
    except Exception as e:
        return AgentResult(
            report="", returncode=1, raw_stdout="", raw_stderr="",
            error=f"Failed to spawn agent: {e}",
        )
    finally:
        # Clean up temp dir (settings only, no important state).
        shutil.rmtree(work_dir, ignore_errors=True)

    # Extract the agent's response.
    report = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                report = payload.get("result", "")
                break
            except json.JSONDecodeError:
                continue

    if not report:
        # Try raw stdout if JSON parsing failed.
        report = proc.stdout[:5000] if proc.stdout else ""

    return AgentResult(
        report=report,
        returncode=proc.returncode,
        raw_stdout=proc.stdout[:5000],
        raw_stderr=proc.stderr[:2000],
        error=None if proc.returncode == 0 else f"Agent exited with code {proc.returncode}",
    )


def _write_agent_settings(work_dir: Path, db_path: Path) -> None:
    """Write .claude/settings.local.json granting the agent the tools it needs."""
    settings_dir = work_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "permissions": {
            "allow": [
                "Read",
                "Grep",
                "Glob",
                "Bash(engram *)",
                "Bash(sqlite3 *)",
                "Bash(wc *)",
                "Bash(head *)",
                "Bash(cat *)",
                "Bash(ls *)",
            ]
        }
    }
    (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))


def _get_memory_summary(db_path: Path) -> str:
    """Get current memory state for the agent's context."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    memories = conn.execute(
        "SELECT m.id, m.name, m.body, m.type, m.surface_count, m.useful_count "
        "FROM memories m WHERE m.archived_ts IS NULL ORDER BY m.id"
    ).fetchall()

    lines = [f"Active memories ({len(memories)}):"]
    for m in memories:
        usefulness = (m["useful_count"] + 1.0) / (m["surface_count"] + 2.0)
        lines.append(
            f"  [{m['id']}] \"{m['name']}\" type={m['type']} "
            f"surfaces={m['surface_count']} useful={m['useful_count']} "
            f"usefulness={usefulness:.2f}"
        )
        lines.append(f"       body: {m['body'][:150]}")

    # Recent surfaces.
    surfaces = conn.execute(
        "SELECT ss.memory_id, m.name, ss.session_id, ss.hook "
        "FROM session_surfaces ss JOIN memories m ON m.id = ss.memory_id "
        "ORDER BY ss.surfaced_ts DESC LIMIT 20"
    ).fetchall()
    lines.append(f"\nRecent surfaces ({len(surfaces)}):")
    for s in surfaces:
        lines.append(f"  memory={s['memory_id']} \"{s['name']}\" session={s['session_id'][:12]}... hook={s['hook']}")

    conn.close()
    return "\n".join(lines)


