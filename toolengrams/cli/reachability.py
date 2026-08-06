"""engram reachability — which memories can still match a real tool call?

A memory that matches nothing is invisible to every existing metric: `q` only
moves when a memory surfaces, so an unreachable memory keeps a healthy score
forever. This replays the historical tool-call corpus (every wired target's
own sessions, via the adapter seam) through the PRODUCTION matcher —
`target.extract_hints` -> `retrieval.rank.retrieve_candidates` — and reports
what nothing can reach.

Full-corpus scan, tens of seconds. CLI only: never a hook, never PreToolUse.

`--archive` is opt-in and routes every candidate through `archivable()`, which
is a stack of vetoes rather than a filter: a `block` is a safety control that
has never fired precisely BECAUSE the dangerous command never came up, and a
trigger whose command still appears in the corpus is over-specific (widen it),
not dead. Default is dry-run.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import date, timedelta

from .. import db, envvars, memory_store
from ..consolidation.agent import COLD_MEMORY_DAYS
from ..harness_names import CLAUDE_CODE
from ..retrieval.rank import retrieve_candidates
from ..target import TARGETS
from ..utils import env_int, project_slug_for_cwd
from .consolidate import collect_sessions

# How far back the corpus reaches. Wider than the cold horizon on purpose: the
# window decides what counts as reachable, and a narrow one calls a quarterly
# command dead. The minimum age to be archivable stays the cold horizon
# (ENGRAM_COLD_MEMORY_DAYS) — "has it had a chance to fire" is already a
# settled question in this codebase.
DEFAULT_DAYS = 90

# Archiving trusts "matched nothing" to mean "unreachable". That inference is
# only valid if we actually read a corpus: a target that stops being wired makes
# collect_corpus() return [] silently, and then EVERY memory looks dead. Refuse
# to archive below this many distinct observed tokens.
# ponytail: crude floor — catches the empty corpus, not a half-read one.
MIN_ARCHIVE_TOKENS = 50


def collect_corpus(days: int) -> list:
    """Sessions from every wired target over the last `days` days, tagged.

    Reuses the nightly collector, so a target that is wired for consolidation
    is automatically part of the corpus. ponytail: re-globs per day because
    collect_sessions takes one date; give it a range if that ever matters.
    """
    today = date.today()
    return [s for n in range(days + 1)
            for s in collect_sessions(today - timedelta(days=n))]


def scan(conn, sessions) -> tuple[set[int], set[str]]:
    """Replay the corpus once. Returns (reachable memory ids, tokens observed).

    Reachable = the production matcher returns the memory for at least one real
    call (pre-gate, pre-cap: "can it match at all", not "would pretool show it").
    The observed-token set is the archive gate's second opinion — it is not
    scoped and not anchored on the call's first token, so it still sees a
    command the matcher itself cannot reach (`sudo ipconfig set ...`).
    """
    reachable: set[int] = set()
    tokens_seen: set[str] = set()
    seen: set[tuple] = set()
    slugs: dict[str, str | None] = {}
    for session in sessions:
        target = TARGETS.get(session.target) or TARGETS[CLAUDE_CODE]
        try:
            calls = list(target.iter_tool_calls(session.path))
        except OSError:
            continue
        for tool_name, tool_input, cwd in calls:
            key = (tool_name, cwd, json.dumps(tool_input, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            hint = target.extract_hints(tool_name, tool_input)
            tokens_seen.update(t.lower() for t in hint.tokens)
            if not hint.tokens and not hint.paths:
                continue
            if cwd not in slugs:
                # use_git=False — exactly what pretool does on the match side.
                slugs[cwd] = project_slug_for_cwd(cwd, use_git=False) if cwd else None
            for candidate in retrieve_candidates(conn, hint, slugs[cwd]):
                reachable.add(candidate.memory_id)
    return reachable, tokens_seen


def archivable(memories, triggers_by_mem, tokens_seen, cutoff_ts) -> tuple[list, Counter]:
    """The auto-archive gate. Returns (candidates, why-the-rest-were-spared).

    Every clause is a veto and `--archive` is the only caller, so none of them
    can be bypassed. Beyond kind/pinned/age, two vetoes exist because "matched
    nothing" has causes that are not "the knowledge is dead":
      - a path_glob trigger matched no file merely because that file wasn't
        touched in the window (and a pattern lacking a leading `/`, `~` or `*`
        can never match at all — a formation defect to repair, not a memory to
        delete);
      - the trigger's command still shows up in the corpus, so the chain is
        over-specific — the remedy is `engram trigger`, not archiving.
    """
    out, spared = [], Counter()
    for m in memories:
        triggers = triggers_by_mem.get(m.id, [])
        if m.kind == "block":
            spared["block_safety_control"] += 1        # never, under any flag
        elif m.pinned:
            spared["pinned"] += 1
        elif m.created_ts >= cutoff_ts:
            spared["younger_than_cold_horizon"] += 1
        elif not triggers or any(t.kind == "path_glob" for t in triggers):
            spared["path_glob_trigger"] += 1
        elif any((t.first_token or "").lower() in tokens_seen for t in triggers):
            spared["command_still_in_use_widen_trigger"] += 1
        else:
            out.append(m)
    return out, spared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram reachability")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"How many days of tool calls to replay (default {DEFAULT_DAYS}).")
    parser.add_argument("--archive", action="store_true",
                        help="Actually archive the candidates (default: dry run). "
                             "Blocks and pinned memories are never archived.")
    args = parser.parse_args(argv)

    days = max(1, args.days)
    with db.session() as conn:
        memories = memory_store.list_memories(conn)
        triggers_by_mem = memory_store.triggers_by_memory(conn)
        reachable, tokens_seen = scan(conn, collect_corpus(days))
        dead = [m for m in memories if m.id not in reachable]
        never = [m for m in dead if m.surface_count == 0]
        cold_days = max(1, env_int(envvars.COLD_MEMORY_DAYS, COLD_MEMORY_DAYS))
        candidates, spared = archivable(never, triggers_by_mem, tokens_seen,
                                        int(time.time()) - cold_days * 86400)
        if args.archive:
            if len(tokens_seen) < MIN_ARCHIVE_TOKENS:
                print(json.dumps({
                    "error": "corpus too small to archive safely",
                    "tokens_observed": len(tokens_seen),
                    "minimum": MIN_ARCHIVE_TOKENS,
                    "hint": "no wired target produced sessions — run `engram doctor`",
                }, indent=2))
                return 1
            for m in candidates:
                memory_store.archive(conn, m.id)

        out = {
            "window_days": days,
            "min_age_days": cold_days,
            "active": len(memories),
            "reachable": len(memories) - len(dead),
            "reachability_pct": round(100.0 * (len(memories) - len(dead)) / max(1, len(memories)), 1),
            "dead_never_surfaced": len(never),
            # Surfaced before, dead now: a narrowing overshot. Repair, don't archive.
            "dead_previously_surfaced": len(dead) - len(never),
            "spared": dict(spared),
            "archived" if args.archive else "would_archive":
                [{"id": m.id, "name": m.name} for m in candidates],
        }
    print(json.dumps(out, indent=2))
    return 0
