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
from ..queries import get_memory_summary_detailed
from ..subprocess_utils import parse_claude_json_output, write_agent_settings
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
    write_agent_settings(work_path, [
        "Read", "Grep", "Glob",
        "Bash(engram *)", "Bash(sqlite3 *)",
        "Bash(wc *)", "Bash(head *)", "Bash(cat *)", "Bash(ls *)",
    ])

    # Build the prompt.
    memory_summary = get_memory_summary_detailed(db_path)
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



