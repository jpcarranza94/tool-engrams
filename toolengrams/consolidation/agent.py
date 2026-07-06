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
from ..models import Trigger
from ..prompts.consolidation import (
    build_consolidation_prompt,
    build_consolidation_retry_prompt,
)
from ..retrieval import session_state
from ..watcher import runs_store
from ..reinforcement.scoring import q
from ..utils import env_int, prepend_engram_bin
from ..target.interface import SessionFile
from . import report_parse, runs

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

# Inventory/cold body snippets are truncated to this; the agent can
# `engram recall --id N` for the full text.
BODY_SNIPPET_CHARS = 500

# Per-section character sub-budget for the enriched summary sections (WS4.4:
# dup clusters, narrow-or-archive candidates, cold detail). MAX_TOTAL_BYTES is
# the whole-session budget (10 MB) — far too coarse to keep any single section
# from crowding the transcripts out of the agent's context, so each enriched
# list caps itself and appends an "N more omitted" marker past the cap.
MAX_SUMMARY_SECTION_CHARS = 6000


def _mem_label(m) -> str:
    """The `[id] "name"` prefix shared by every memory row in the summary."""
    return f'[{m.id}] "{m.name}"'


def _body_line(m) -> str:
    """The indented body-snippet line rendered under a memory row."""
    return f"       body: {m.body[:BODY_SNIPPET_CHARS]}"


def _trigger_signature(t: Trigger) -> tuple | None:
    """A hashable dedup key for one trigger: the token-SET (order-independent) for
    a token_subseq trigger, or the exact pattern for a path_glob. None for a
    trigger carrying neither (nothing to cluster on)."""
    if t.kind == "token_subseq":
        toks = t.tokens
        return ("tokens", frozenset(toks)) if toks else None
    if t.kind == "path_glob" and t.path_pattern:
        return ("path", t.path_pattern)
    return None


def _signature_label(sig: tuple) -> str:
    kind, val = sig
    if kind == "tokens":
        return "tokens {" + ", ".join(sorted(val)) + "}"
    return f"path {val}"


def _trigger_labels(triggers: list[Trigger]) -> str:
    """Compact one-line rendering of a memory's triggers for the cold detail."""
    parts = []
    for t in triggers:
        if t.kind == "token_subseq" and t.tokens:
            parts.append("[" + " ".join(t.tokens) + "]")
        elif t.kind == "path_glob" and t.path_pattern:
            parts.append(f"path:{t.path_pattern}")
    return ", ".join(parts) if parts else "(no triggers)"


def _render_cluster(entry) -> str:
    """One dup-cluster row: the shared signature → its member memories."""
    sig, mems = entry
    members = "; ".join(_mem_label(m) for m in mems)
    return f"  shared {_signature_label(sig)} → {members}"


def _dup_clusters(memories: list, triggers_by_mem: dict[int, list[Trigger]]) -> list:
    """Active memories that share an identical trigger signature (token-set or
    path pattern), as `[(signature, [memories])]` for signatures owned by 2+
    memories. These are FOLD-or-NARROW candidates, not automatic merges — the
    agent decides whether the bodies are the same fact."""
    sig_to_mems: dict[tuple, list] = {}
    for m in memories:
        seen: set[tuple] = set()
        for t in triggers_by_mem.get(m.id, ()):
            sig = _trigger_signature(t)
            if sig is None or sig in seen:
                continue
            seen.add(sig)
            sig_to_mems.setdefault(sig, []).append(m)
    return [(sig, mems) for sig, mems in sig_to_mems.items() if len(mems) >= 2]


def _append_bounded(lines: list, items: list, render, *,
                    budget: int = MAX_SUMMARY_SECTION_CHARS) -> None:
    """Append `render(item)` for each item until `budget` chars are used, then
    append a single "N more omitted" marker. Always shows at least one item."""
    used = 0
    for shown, item in enumerate(items):
        text = render(item)
        if used + len(text) > budget and shown:
            lines.append(f"  ... ({len(items) - shown} more omitted for budget)")
            return
        lines.append(text)
        used += len(text)


def _bounded_section(lines: list, header: str, items: list, render) -> None:
    """Append `header` then the budget-bounded rendering of `items`; a no-op when
    `items` is empty, so callers skip the empty-check boilerplate."""
    if not items:
        return
    lines.append(header)
    _append_bounded(lines, items, render)


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
    triggers_by_mem = memory_store.triggers_by_memory(conn)
    # Per-memory helpful/unused/noise split from the raw judged surfaces — the
    # memory row only carries useful/noise counters; `unused` lives only here.
    dist = session_state.outcome_distribution(conn)
    # q per memory, computed once here and reused by the flagged filter/render below.
    qmap = {m.id: q(m.useful_count, m.noise_count) for m in memories}

    lines = [f"Active memories ({len(memories)}) ordered audit-first (never-verified, then oldest-verified):"]
    for m in memories:
        scope_str = m.scope
        if m.project_slug:
            scope_str = f"{scope_str}:{m.project_slug}"
        verified_str = f"verified={m.last_verified_ts}" if m.last_verified_ts else "verified=never"
        # helpful == useful_count; noise == noise_count; unused is surfaces the
        # agent saw but did not act on (does NOT count against q).
        unused = dist.get(m.id, {}).get("unused", 0)
        lines.append(
            f"  {_mem_label(m)} kind={m.kind} "
            f"scope={scope_str} "
            f"surfaces={m.surface_count} helpful={m.useful_count} unused={unused} "
            f"noise={m.noise_count} q={qmap[m.id]:.2f} created={m.created_ts} {verified_str}"
        )
        lines.append(_body_line(m))

    # Narrow-or-archive candidates: q<0.5 AND noise-dominant (more noise verdicts
    # than helpful). The surfacing gate already suppresses these hints, so they're
    # dead weight until fixed — prefer NARROWING the over-matching trigger (Task 2)
    # over archiving unless the content itself is useless.
    flagged = [m for m in memories
               if qmap[m.id] < 0.5 and m.noise_count > m.useful_count]
    _bounded_section(
        lines,
        f"\nNarrow-or-archive candidates ({len(flagged)}) — q<0.5 and noise-dominant. "
        "The gate already suppresses these. Prefer trigger-narrowing over archiving "
        "unless the body is useless:",
        flagged,
        lambda m: (f"  {_mem_label(m)} q={qmap[m.id]:.2f} "
                   f"helpful={m.useful_count} noise={m.noise_count} "
                   f"triggers: {_trigger_labels(triggers_by_mem.get(m.id, []))}"),
    )

    # Duplicate trigger clusters: active memories sharing an identical trigger
    # token-set or path pattern. Fold ONLY when the bodies are the SAME fact;
    # when they're distinct facts that happen to share a broad trigger, NARROW /
    # scope the trigger instead (folding distinct facts loses knowledge).
    clusters = _dup_clusters(memories, triggers_by_mem)
    _bounded_section(
        lines,
        f"\nDuplicate trigger clusters ({len(clusters)}) — memories sharing an identical "
        "trigger. Fold ONLY true same-fact duplicates (prefer the broader-reach "
        "survivor per Task 2); if they are DISTINCT facts sharing a broad trigger, do "
        "NOT fold — narrow/scope the shared trigger so each fires precisely:",
        clusters,
        _render_cluster,
    )

    # Cold (never-surfaced) memories — listed separately so the agent triages
    # them instead of losing them among the inventory rows above. Each carries its
    # body snippet + trigger list inline so the agent can diagnose (trigger can't
    # match vs. pattern doesn't recur) without an extra recall round-trip.
    # Clamp to >= 1: a 0/negative horizon would move the cutoff to now-or-future
    # and flag every just-created never-surfaced memory as cold — the exact
    # false-positive-archive failure mode the conservative default guards against.
    cold_days = max(1, env_int(envvars.COLD_MEMORY_DAYS, COLD_MEMORY_DAYS))
    cold = _cold_memories(memories, now - cold_days * 86400)
    def _render_cold(m):
        return (f"  {_mem_label(m)} triggers: "
                f"{_trigger_labels(triggers_by_mem.get(m.id, []))}\n"
                f"{_body_line(m)}")
    _bounded_section(
        lines,
        f"\nCold — never surfaced in {cold_days}+ days ({len(cold)}). The trigger has "
        "had time to match a live call and never did. TRIAGE each (see Task 2): fix the "
        "trigger if it can't match the real command, `engram forget --delete` if the "
        "pattern won't recur, or leave genuinely-useful-but-rare facts alone:",
        cold,
        _render_cold,
    )

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

    # Standing recommendation backlog (WS5.1): the OPEN advisories you (or a prior
    # run) already raised. Without this you re-raise recurring issues as fresh
    # `open` rows nightly. Per Task 6, re-emit each title UNCHANGED with the SAME
    # casefolded label — status `done` if THIS run resolved it, else `open`.
    # (Critical / code-bug items are intentionally absent — those close only via
    # `engram recommend --close`, since you can't verify a code fix shipped.)
    backlog = runs.open_recommendations(conn, runs.OPEN_BACKLOG_RUN_WINDOW)
    if backlog:
        lines.append(
            f"\nStanding recommendation backlog ({len(backlog)} open) — re-affirm or "
            "resolve each in your Task 6 output (reuse the EXACT title):"
        )
        for r in backlog:
            # Cap agent-authored detail like the quarantines section ([:200]) so a
            # verbose backlog can't crowd out the transcripts — the one enriched
            # section not routed through _bounded_section (every title must show
            # for Task 6 re-affirmation, so we trim each row rather than the list).
            detail = f" — {r['detail'][:200]}" if r["detail"] else ""
            lines.append(f"  ({r['severity']}) \"{r['title']}\"{detail}")

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


