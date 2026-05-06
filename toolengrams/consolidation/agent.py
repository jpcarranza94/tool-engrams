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
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..prompts.consolidation import build_consolidation_prompt
from ..reinforcement.scoring import usefulness
from ..subprocess_utils import parse_claude_json_output, write_agent_settings
from .collect import SessionFile

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Session budget for the consolidation agent. Prevents timeout on heavy days.
MAX_SESSIONS = 10
MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 10 MB total
MAX_SINGLE_SESSION_BYTES = 5 * 1024 * 1024  # 5 MB per session — skip giants


def _find_claude() -> str | None:
    """Resolve claude binary at call time, not import time.

    Module-level shutil.which() fails when the module is imported before
    PATH is fully set (launchd's minimal environment).
    """
    return shutil.which("claude")


def _get_memory_summary(db_path: Path) -> str:
    """Detailed memory state for consolidation agent context.

    Opens its own connection because the consolidation agent runs in a
    subprocess with only a path, not a shared connection.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db_path))
    conn.row_factory = _sqlite3.Row

    memories = conn.execute(
        "SELECT m.id, m.name, m.body, m.kind, m.surface_count, m.useful_count "
        "FROM memories m WHERE m.archived_ts IS NULL ORDER BY m.id"
    ).fetchall()

    lines = [f"Active memories ({len(memories)}):"]
    for m in memories:
        u = usefulness(m["useful_count"], m["surface_count"])
        lines.append(
            f"  [{m['id']}] \"{m['name']}\" kind={m['kind']} "
            f"surfaces={m['surface_count']} useful={m['useful_count']} "
            f"usefulness={u:.2f}"
        )
        lines.append(f"       body: {m['body'][:150]}")

    surfaces = conn.execute(
        "SELECT ss.memory_id, m.name, ss.session_id, ss.hook "
        "FROM session_surfaces ss JOIN memories m ON m.id = ss.memory_id "
        "ORDER BY ss.surfaced_ts DESC LIMIT 20"
    ).fetchall()
    lines.append(f"\nRecent surfaces ({len(surfaces)}):")
    for s in surfaces:
        lines.append(
            f"  memory={s['memory_id']} \"{s['name']}\" "
            f"session={s['session_id'][:12]}... hook={s['hook']}"
        )

    conn.close()
    return "\n".join(lines)


def _prioritize_sessions(sessions: list[SessionFile]) -> list[SessionFile]:
    """Select the most important sessions within budget.

    Sort by size descending (larger sessions = more substantive work),
    skip sessions over MAX_SINGLE_SESSION_BYTES (too large for the agent
    to process in time), take up to MAX_SESSIONS or MAX_TOTAL_BYTES.
    """
    # Filter out giant sessions the agent can't process in 30 min.
    eligible = [s for s in sessions if s.size_bytes <= MAX_SINGLE_SESSION_BYTES]
    sorted_sessions = sorted(eligible, key=lambda s: -s.size_bytes)
    selected: list[SessionFile] = []
    total = 0
    for s in sorted_sessions:
        if len(selected) >= MAX_SESSIONS:
            break
        if total + s.size_bytes > MAX_TOTAL_BYTES and selected:
            break
        selected.append(s)
        total += s.size_bytes
    return selected


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
    claude_bin = _find_claude()
    if not claude_bin:
        return AgentResult(
            report="", returncode=1, raw_stdout="", raw_stderr="",
            error="claude CLI not found on PATH",
        )

    if not sessions:
        return AgentResult(
            report="No sessions to review.", returncode=0,
            raw_stdout="", raw_stderr="",
        )

    # Cap sessions to prevent timeout on heavy days.
    sessions = _prioritize_sessions(sessions)

    # Build the agent's working environment.
    work_dir = tempfile.mkdtemp(prefix="engram-consolidate-")
    work_path = Path(work_dir)
    write_agent_settings(work_path, [
        "Read", "Grep", "Glob",
        "Bash(engram *)", "Bash(sqlite3 *)",
        "Bash(wc *)", "Bash(head *)", "Bash(cat *)", "Bash(ls *)",
    ])

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
            [claude_bin, "-p", "--bare", "--output-format", "json", "--", prompt],
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
    report = parse_claude_json_output(proc.stdout)
    if not report:
        report = proc.stdout[:5000] if proc.stdout else ""

    return AgentResult(
        report=report,
        returncode=proc.returncode,
        raw_stdout=proc.stdout[:5000],
        raw_stderr=proc.stderr[:2000],
        error=None if proc.returncode == 0 else f"Agent exited with code {proc.returncode}",
    )



