"""Candidate retrieval — tool call → scored candidates.

Matching model:
  - token_subseq: the call's command heads (`call_anchors`) select buckets via
    one indexed lookup, then we subsequence-match stored trigger tokens against
    the call's tokens in Python.
  - path_glob: fnmatch the stored pattern against each extracted call path.

Scoring is applied by `reinforcement/scoring.py::final_score`; this module
just reads candidates, runs the match predicate, and attaches the score.
Session dedup lives in the hook handlers themselves; the final sort + cap is
shared by both of them in hooks/_skip.py::rank_and_cap. There is no cluster-level
Laplace gate — the two-kind model makes per-cluster quality filtering
redundant.
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import time

from .. import memory_store
from ..models import Candidate
from ..reinforcement.scoring import final_score
from .extract import ExtractedTriggerHint, access_mode_for_tool


def retrieve_candidates(
    conn: sqlite3.Connection,
    hint: ExtractedTriggerHint,
    project_slug: str | None,
    kind: str | None = None,
) -> list[Candidate]:
    """Return memories whose triggers match this tool call.

    Scope filter: (scope='global') OR (scope='project' AND project_slug=?).
    Archived memories excluded.

    If `kind` is provided ('block' or 'hint'), only memories of that kind are
    returned — pretool asks for blocks, PostToolUseFailure asks for hints.
    """
    candidates: dict[int, Candidate] = {}

    # --- token_subseq matches ---
    if hint.tokens:
        rows = memory_store.match_token_triggers(
            conn, call_anchors(hint.tokens), project_slug, kind)
        call = tuple(hint.tokens)
        for row in rows:
            stored = _load_tokens(row["tokens_json"])
            if not stored:
                continue
            if is_subsequence(stored, call):
                _merge_token_candidate(candidates, row, stored)

    # --- path_glob matches ---
    if hint.paths:
        access_mode = access_mode_for_tool(hint.tool_name)
        rows = memory_store.match_path_triggers(conn, project_slug, kind, access_mode)
        for row in rows:
            if _any_path_matches(row["path_pattern"], hint.paths):
                _merge_path_candidate(candidates, row)

    for c in candidates.values():
        c.final_score = final_score(c)

    return list(candidates.values())


# Shell separators shlex leaves as standalone tokens.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})


def call_anchors(tokens: list[str]) -> list[str]:
    """The call's command heads — `cd x && sudo gh run` → ["cd", "sudo", "gh"].

    Only tokens[0] used to select the indexed bucket, so `sudo ipconfig ...`
    anchored on "sudo" and `cd ~/x && gh run list` on "cd" — an `ipconfig` or
    `gh` trigger was never even considered. Verbatim, no case folding:
    first_token is stored as-is (Make != make, and the live corpus has
    triggers anchored on `Agent`, `Workflow`, `C0ASKPH84P8`).
    """
    out: list[str] = []
    head = True
    for tok in tokens:
        if tok in _SEPARATORS:
            head = True
        elif head:
            if tok not in out:
                out.append(tok)
            # `sudo cmd` / `FOO=bar cmd`: the real command is the next token.
            head = tok == "sudo" or "=" in tok
    # Bounded so a pathological generated pipeline can't blow past sqlite's
    # variable limit and silently fail-open the hook. Measured over all 31,722
    # real corpus Bash calls: mean 2.42 anchors, p95 5, max 39 — so 64 leaves
    # the cap non-binding in practice while still bounding the query.
    return out[:64]


def is_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    """All tokens in `needle` appear in `haystack` in order (non-contiguous allowed).

    `["mycli", "order", "reassign"]` matches `mycli order 12345 reassign`
    because "12345" is simply skipped.
    """
    if not needle:
        return False
    it = iter(haystack)
    return all(token in it for token in needle)


def _load_tokens(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(x) for x in parsed)


def _any_path_matches(pattern: str | None, paths: list[str]) -> bool:
    if not pattern:
        return False
    return any(fnmatch.fnmatchcase(p, pattern) for p in paths)


def _merge_token_candidate(
    store: dict[int, Candidate],
    row: sqlite3.Row,
    stored_tokens: tuple[str, ...],
) -> None:
    """Keep the most specific (longest) matching trigger per memory."""
    existing = store.get(row["id"])
    if existing is None or len(stored_tokens) > len(existing.matched_tokens):
        store[row["id"]] = Candidate(
            memory_id=row["id"],
            name=row["name"],
            body=row["body"],
            matched_tokens=stored_tokens,
            matched_path=None,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            noise_count=row["noise_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            kind=row["kind"],
            scope=row["scope"],
            origin_session_id=row["origin_session_id"],
        )


def _merge_path_candidate(store: dict[int, Candidate], row: sqlite3.Row) -> None:
    if row["id"] in store:
        return
    store[row["id"]] = Candidate(
        memory_id=row["id"],
        name=row["name"],
        body=row["body"],
        matched_tokens=(),
        matched_path=row["path_pattern"],
        surface_count=row["surface_count"],
        useful_count=row["useful_count"],
        noise_count=row["noise_count"],
        last_surfaced_ts=row["last_surfaced_ts"],
        pinned=bool(row["pinned"]),
        kind=row["kind"],
        scope=row["scope"],
        origin_session_id=row["origin_session_id"],
    )


def now() -> int:
    return int(time.time())
