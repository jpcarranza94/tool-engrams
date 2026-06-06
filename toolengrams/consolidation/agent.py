"""Agent-based consolidation: spawn an Opus agent to review today's sessions.

Instead of a brittle pipeline (regex → truncated episodes → JSON prompt),
we give an Opus agent the raw session files, the engram CLI, and let it
explore freely. The agent reads transcripts, evaluates memory surfacing
quality, identifies missed corrections, and runs engram commands directly.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import memory_store
from ..claude_invoke import invoke_claude_agent, parse_claude_json_output, write_agent_settings
from ..prompts.consolidation import build_consolidation_prompt
from ..retrieval import session_state
from ..reinforcement.scoring import q
from .collect import SessionFile

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Session budget for the consolidation agent. Prevents timeout on heavy days.
MAX_SESSIONS = 10
MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 10 MB total
MAX_SINGLE_SESSION_BYTES = 5 * 1024 * 1024  # 5 MB per session — skip giants

# Per-run wall-clock budget for the consolidation agent's `claude -p`.
CONSOLIDATION_TIMEOUT_SEC = 1800  # 30 minutes


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
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Audit-first ordering (never-verified, then oldest-verified) puts the most
    # audit-worthy memories at the top of the agent's context so a truncated
    # reading still covers the work that matters.
    memories = memory_store.list_memories(conn, order="audit")

    lines = [f"Active memories ({len(memories)}) ordered audit-first (never-verified, then oldest-verified):"]
    for m in memories:
        qv = q(m.useful_count, m.noise_count)
        scope_str = m.scope
        if m.project_slug:
            scope_str = f"{scope_str}:{m.project_slug}"
        verified_str = f"verified={m.last_verified_ts}" if m.last_verified_ts else "verified=never"
        lines.append(
            f"  [{m.id}] \"{m.name}\" kind={m.kind} "
            f"scope={scope_str} "
            f"surfaces={m.surface_count} useful={m.useful_count} noise={m.noise_count} "
            f"q={qv:.2f} created={m.created_ts} {verified_str}"
        )
        # Body truncated to 500 chars; agent can `engram recall --id N` for full text.
        lines.append(f"       body: {m.body[:500]}")

    surfaces = session_state.recent_surfaces_with_memory(conn, limit=20)
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
            report="", returncode=1,
            error="claude CLI not found on PATH",
        )

    if not sessions:
        return AgentResult(report="No sessions to review.", returncode=0)

    # Cap sessions to prevent timeout on heavy days.
    sessions = _prioritize_sessions(sessions)

    # Build the agent's working environment.
    work_dir = tempfile.mkdtemp(prefix="engram-consolidate-")
    work_path = Path(work_dir)
    write_agent_settings(work_path, [
        "Read", "Grep", "Glob",
        "Bash(engram *)", "Bash(sqlite3 *)",
        "Bash(wc *)", "Bash(head *)", "Bash(cat *)", "Bash(ls *)",
        # Read-only git inspection so the agent can compare memory bodies
        # against current repo state (Task 5 — git-aware staleness audit).
        "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
        "Bash(git -C *)", "Bash(git rev-parse *)",
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

    # invoke_claude_agent never raises — process failures come back on the result.
    result = invoke_claude_agent(
        prompt,
        timeout=CONSOLIDATION_TIMEOUT_SEC,
        cwd=work_dir,
        env=env,
        claude_bin=claude_bin,
    )
    # Clean up temp dir (settings only, no important state).
    shutil.rmtree(work_dir, ignore_errors=True)

    if result.timed_out:
        return AgentResult(
            report="", returncode=1,
            error=f"Consolidation agent timed out ({CONSOLIDATION_TIMEOUT_SEC // 60} min)",
        )
    if result.error:
        return AgentResult(report="", returncode=1, error=f"Failed to spawn agent: {result.error}")

    # Extract the agent's response.
    report = parse_claude_json_output(result.stdout)
    if not report:
        report = result.stdout[:5000] if result.stdout else ""

    return AgentResult(
        report=report,
        returncode=result.returncode,
        error=None if result.returncode == 0 else f"Agent exited with code {result.returncode}",
    )



