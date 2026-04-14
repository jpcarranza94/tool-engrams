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
    prompt = _build_agent_prompt(sessions, memory_summary, target_date)

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


def _build_agent_prompt(
    sessions: list[SessionFile],
    memory_summary: str,
    target_date: str,
) -> str:
    # Build session file list with sizes.
    session_list = "\n".join(
        f"  {s.path} ({s.size_bytes / 1024:.0f} KB) — session {s.session_id[:12]}..."
        for s in sessions
    )

    return f"""You are the nightly consolidation agent for ToolEngrams — a tool-bound memory system for Claude Code.

Your job is to review today's ({target_date}) sessions and evaluate how well the memory system performed. Think of this as "sleep consolidation" — replaying the day's experiences to strengthen good memories and prune bad ones.

## Current Memory State

{memory_summary}

## Today's Session Files

These are JSONL transcripts from today. Each line is a JSON object with a "message" field containing "role" (user/assistant) and "content" (text or tool_use/tool_result blocks). Memory injections appear as system-reminder blocks containing "PreToolUse" and "[memory: ...]".

{session_list}

**Triage strategy**: Start with the larger sessions (>100 KB) — those are real work sessions with substantive tool usage. Small sessions (<20 KB) are often quick one-off questions or automated tests — scan them with Grep but don't deep-read unless something interesting shows up. Focus your time on sessions where the user was actively using tools.

## Your Tasks

### 1. Evaluate existing memory surfacings

Use Grep to find "PreToolUse" and "[memory:" in the JSONL files. For each surfacing:
- Was the memory relevant to what the user was actually doing?
- Did it influence Claude's behavior?
- Was it noise?

### 2. Discover new memories from the day's work

This is the most important task. Go beyond corrections — look for **patterns** the engineer relies on that would be valuable to remember. Specifically:

**Tool-usage patterns**: Commands the user or Claude ran repeatedly, specific flags or options that matter, workflows that follow a consistent sequence. If Claude had to figure out how to run something and the user confirmed it worked, that's a memory.

**Context that had to be rediscovered**: If the user had to tell Claude "the service runs on port X" or "use this connection string" or "that file is at this path" — and it relates to a tool call — that's a memory that should persist.

**Project-specific tool configurations**: Build commands, test commands, deployment steps, database access patterns. Things like "`make test` before pushing in this repo" or "`docker compose -f docker-compose.dev.yml up`".

**Corrections**: "Don't do X, do Y instead" about specific commands.

**Confirmed approaches**: When the user said "yes that's right" or accepted a non-obvious tool-call choice without pushback.

### 3. Take action

- For noisy memories: `engram forget "<name>"`
- For new discoveries: `engram remember "<body>" --type <feedback|reference> --scope <global|project> --name "<name>"`
  - Body MUST include backticked commands or file paths (triggers are extracted from these)
  - type=feedback for corrections/preferences, type=reference for how-to-use facts
  - scope=global if it applies everywhere, scope=project if it's repo-specific

### 4. Write a consolidation report

Your final response should include:
- Sessions reviewed and what kind of work happened
- Memory surfacing evaluations (helpful/noise/neutral)
- New memories created and why
- Memories demoted and why
- Observations about the memory system's performance

## Tools Available

- `Read` — read JSONL files
- `Grep` — search file contents efficiently
- `Bash(engram recall)` — list current memories
- `Bash(engram recall --id N)` — detail on one memory
- `Bash(engram forget "name")` — soft-demote a memory
- `Bash(engram remember "body" --type T --scope S --name "name")` — create a memory
- `Bash(engram status)` — system health

## Guidelines

- Be thorough — read the substantive sessions, not just grep for keywords
- Prioritize discovery of genuinely useful tool-bound patterns over cataloging everything
- Every memory you create must have backticked commands or file paths in the body
- Err on the side of creating memories — it's cheap to forget later, expensive to miss a pattern
- Don't create memories for one-off commands that won't recur
- A good memory answers "what should Claude know next time it runs this tool?"
"""
