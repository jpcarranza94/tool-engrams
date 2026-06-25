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
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .. import envvars, memory_store
from ..engine import EngineRequest, SandboxSpec, get_engine
from ..prompts.consolidation import (
    build_consolidation_prompt,
    build_consolidation_retry_prompt,
)
from ..retrieval import session_state
from ..watcher import runs_store
from ..reinforcement.scoring import q
from ..utils import env_int, prepend_engram_bin
from ..target.interface import SessionFile
from . import report_parse

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# How many times a malformed report JSON block is re-requested in the SAME agent
# session before giving up and storing whatever parsed (a correctness bound, not
# a tunable — each retry is one extra cheap turn).
MAX_REPORT_RETRIES = 2

# Session budget for the consolidation agent. Prevents timeout on heavy days.
MAX_SESSIONS = 10
MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 10 MB total
MAX_SINGLE_SESSION_BYTES = 5 * 1024 * 1024  # 5 MB per session — skip giants

# Per-run wall-clock budget for the consolidation agent's `claude -p`.
CONSOLIDATION_TIMEOUT_SEC = 1800  # 30 minutes

# Never-surfaced + older than this many days = "cold" (see _cold_memories). The
# conservative default keeps freshly-formed memories — which legitimately haven't
# hit their trigger yet — out of the bucket.
COLD_MEMORY_DAYS = 30


def _cold_memories(memories: list, cutoff_ts: int) -> list:
    """Memories that have never surfaced and predate `cutoff_ts`, oldest first.

    A `surface_count == 0` memory past the cold horizon has had real wall-clock
    time to match a live tool call and never did — either its trigger can't match
    how the command is actually typed (fixable) or the pattern simply doesn't
    recur (dead weight). `created_ts` is a proxy for "had a chance to fire": the
    system keeps no per-memory exposure clock, and for a never-surfaced memory
    `last_surfaced_ts` is uninformative (always 0), so creation age is the only
    signal available. Pure filter over the already-loaded list — no extra query.
    """
    return sorted(
        (m for m in memories if m.surface_count == 0 and m.created_ts < cutoff_ts),
        key=lambda m: m.created_ts,
    )


def _get_memory_summary(db_path: Path) -> str:
    """Detailed memory state for consolidation agent context.

    Opens its own connection because the consolidation agent runs in a
    subprocess with only a path, not a shared connection.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = int(time.time())

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

    # Cold (never-surfaced) memories — listed separately so the agent triages
    # them instead of losing them among the inventory rows above (which carry the
    # full kind/scope/created detail; here a bare id+name pointer suffices).
    # Clamp to >= 1: a 0/negative horizon would move the cutoff to now-or-future
    # and flag every just-created never-surfaced memory as cold — the exact
    # false-positive-archive failure mode the conservative default guards against.
    cold_days = max(1, env_int(envvars.COLD_MEMORY_DAYS, COLD_MEMORY_DAYS))
    cold = _cold_memories(memories, now - cold_days * 86400)
    if cold:
        lines.append(
            f"\nCold — never surfaced in {cold_days}+ days ({len(cold)}). The trigger has "
            "had time to match a live call and never did. TRIAGE each (see Task 2): fix the "
            "trigger if it can't match the real command, `engram forget --delete` if the "
            "pattern won't recur, or leave genuinely-useful-but-rare facts alone:"
        )
        lines.extend(f"  [{m.id}] \"{m.name}\"" for m in cold)

    quarantines = runs_store.recent_quarantines(conn, now - 48 * 3600)
    if quarantines:
        lines.append(f"\nQuarantined by the eval watcher (last 48h, {len(quarantines)}) — "
                     "REVIEW EACH: restore (engram forget --restore), repair the body "
                     "(engram edit <id> --body ...) then restore, or leave archived:")
        for ev in quarantines:
            lines.append(f"  [{ev['memory_id']}] \"{ev['memory_name']}\" "
                         f"reason: {(ev['detail'] or '?')[:200]}")

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
    max_sessions = env_int(envvars.CONSOLIDATION_MAX_SESSIONS, MAX_SESSIONS)
    eligible = [s for s in sessions if s.size_bytes <= MAX_SINGLE_SESSION_BYTES]
    sorted_sessions = sorted(eligible, key=lambda s: -s.size_bytes)
    selected: list[SessionFile] = []
    total = 0
    for s in sorted_sessions:
        if len(selected) >= max_sessions:
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
    engine = get_engine()
    if not engine.is_available():
        return AgentResult(
            report="", returncode=1,
            error=f"{engine.NAME} CLI not found on PATH",
        )

    if not sessions:
        return AgentResult(report="No sessions to review.", returncode=0)

    # Cap sessions to prevent timeout on heavy days.
    sessions = _prioritize_sessions(sessions)

    # Build the agent's working environment.
    work_dir = tempfile.mkdtemp(prefix="engram-consolidate-")
    work_path = Path(work_dir)
    # `readonly_explore` carries the broad inspection surface (file tools,
    # sqlite3/wc/head/cat/ls, read-only git for the staleness audit); the one
    # command prefix is the full engram verb set — consolidation is the only
    # agent trusted with it.
    engine.prepare_sandbox(work_path, SandboxSpec(
        command_prefixes=("engram",),
        readonly_explore=True,
    ))

    # Build the prompt.
    memory_summary = _get_memory_summary(db_path)
    session_list = "\n".join(
        f"  [{s.target or 'unknown'}] {s.path} "
        f"({s.size_bytes / 1024:.0f} KB) — session {s.session_id[:12]}..."
        for s in sessions
    )
    prompt = build_consolidation_prompt(session_list, memory_summary, target_date)

    env = prepend_engram_bin(os.environ.copy())
    env["ENGRAM_DB"] = str(db_path)

    timeout_sec = env_int(envvars.CONSOLIDATION_TIMEOUT, CONSOLIDATION_TIMEOUT_SEC)
    # engine.invoke never raises — process failures come back on the result.
    result = engine.invoke(EngineRequest(
        prompt=prompt,
        timeout=timeout_sec,
        role="consolidation",
        cwd=work_dir,
        env=env,
    ))

    if result.timed_out:
        shutil.rmtree(work_dir, ignore_errors=True)
        return AgentResult(
            report="", returncode=1,
            error=f"Consolidation agent timed out ({timeout_sec // 60} min)",
        )
    if result.error:
        shutil.rmtree(work_dir, ignore_errors=True)
        return AgentResult(report="", returncode=1, error=f"Failed to spawn agent: {result.error}")

    # Extract the agent's response, then — if its trailing JSON envelope is
    # malformed — give the SAME session up to a couple of chances to re-emit it
    # before we clean up the sandbox (resume needs the work dir to survive).
    report = _result_report(result)
    report = _retry_invalid_envelope(
        engine, report, result, timeout=timeout_sec, cwd=work_dir, env=env)

    # Clean up temp dir (settings only, no important state).
    shutil.rmtree(work_dir, ignore_errors=True)

    return AgentResult(
        report=report,
        returncode=result.returncode,
        error=None if result.returncode == 0 else f"Agent exited with code {result.returncode}",
    )


def _result_report(result) -> str:
    """The agent's report text: the structured `text`, or the stdout head as a
    last resort (same precedence the recorder always used)."""
    return result.text or (result.stdout[:5000] if result.stdout else "")


def _retry_invalid_envelope(engine, report, primary, *, timeout, cwd, env) -> str:
    """Re-request a malformed report JSON block in the SAME agent session.

    No-op when the report already validates, or when the session can't be
    resumed — the engine returned no session_id (codex runs ephemeral), so there
    is nothing to continue and the lenient parse stands. Bounded by
    MAX_REPORT_RETRIES. A correction call that itself fails or times out is left
    to stand: a flaky retry must never downgrade an otherwise-good run.
    """
    session_id = primary.session_id
    for _ in range(MAX_REPORT_RETRIES):
        if not session_id:
            break
        problems = report_parse.validate_envelope(report_parse.extract_json_block(report))
        if not problems:
            break
        retry = engine.invoke(EngineRequest(
            prompt=build_consolidation_retry_prompt("; ".join(problems)),
            timeout=timeout,
            role="consolidation",
            cwd=cwd,
            env=env,
            resume_session_id=session_id,
        ))
        if retry.returncode != 0 or retry.timed_out or not retry.text:
            break
        report = _merge_corrected(report, _result_report(retry))
        session_id = retry.session_id or session_id
    return report


def _merge_corrected(original: str, correction: str) -> str:
    """Append the correction's JSON block after the original prose so the
    trailing — now valid — block is the one report_parse re-reads, while the
    human-readable report above is preserved. If the correction carries no
    usable block, keep the original unchanged.
    """
    block = report_parse.extract_json_block(correction)
    if not block:
        return original
    return original.rstrip() + "\n\n```json\n" + json.dumps(block, indent=2) + "\n```\n"


