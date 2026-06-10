"""The persistence seam for the Memory aggregate (memories + triggers + FTS).

Every raw SQL statement against `memories`, `triggers`, and `memories_fts` lives
here — nowhere else. Reads return typed `Memory` / `Trigger` objects so callers
never touch column-name strings; a column rename is a one-file change. Writes
(insert/update/delete, the reinforcement counter bumps, trigger persistence) go
through the named functions below so there is a single definition of each
mutation.

Convention: every function takes an open `conn` as its first argument (callers
own the `db.session()` / transaction lifecycle), matching the rest of the code.

Hot path: `match_token_triggers` / `match_path_triggers` return raw rows, not
`Memory` objects — the PreToolUse match runs on every tool call and rank.py
builds the lean `Candidate` from these directly (no per-call allocation).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Sequence

from .models import Memory, Trigger

# Full column list for Memory.from_row — every column the Memory dataclass
# reads. `from_row` uses KEYED access, so order here is irrelevant; only presence
# matters. Adding a field to Memory without adding its column here surfaces as a
# KeyError, which test_memory_store.test_insert_and_get_roundtrip catches.
_MEM_COLS = (
    "id, name, description, body, kind, scope, project_slug, created_ts, "
    "last_surfaced_ts, surface_count, useful_count, noise_count, pinned, "
    "archived_ts, last_verified_ts"
)


def _cols(alias: str) -> str:
    """_MEM_COLS qualified with a table alias (for JOIN queries)."""
    return ", ".join(f"{alias}.{c.strip()}" for c in _MEM_COLS.split(","))

# Soft-demote penalty: phantom surfaces that crater the usefulness ratio
# without fully hiding the memory.
SOFT_DEMOTE_PENALTY = 5


# ---------- search helpers ----------


def fts_quote(text: str) -> str:
    """Turn a search string into a safe FTS5 OR query."""
    tokens = text.split()
    return " OR ".join(f'"{t}"' for t in tokens if t)


# ---------- reads (return Memory / Trigger) ----------


def get(conn: sqlite3.Connection, memory_id: int) -> Memory | None:
    row = conn.execute(
        f"SELECT {_MEM_COLS} FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    return Memory.from_row(row) if row else None


def find_by_name(conn: sqlite3.Connection, name: str,
                 include_archived: bool = False) -> Memory | None:
    """Resolve a memory by name: exact → FTS → LIKE. Returns Memory or None."""
    archived_clause = "" if include_archived else "AND archived_ts IS NULL"

    row = conn.execute(
        f"SELECT {_MEM_COLS} FROM memories WHERE name = ? {archived_clause}",
        (name,),
    ).fetchone()
    if row:
        return Memory.from_row(row)

    if not include_archived:
        fts = fts_quote(name)
        if fts:
            row = conn.execute(
                f"SELECT {_cols('m')} FROM memories m "
                "JOIN memories_fts f ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? AND m.archived_ts IS NULL "
                "ORDER BY rank LIMIT 1",
                (fts,),
            ).fetchone()
            if row:
                return Memory.from_row(row)

    row = conn.execute(
        f"SELECT {_MEM_COLS} FROM memories WHERE name LIKE ? {archived_clause} LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    return Memory.from_row(row) if row else None


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[Memory]:
    """Full-text search over name/description/body (non-archived), rank order."""
    fts = fts_quote(query)
    if not fts:
        return []
    rows = conn.execute(
        f"SELECT {_cols('m')} FROM memories m "
        "JOIN memories_fts f ON m.id = f.rowid "
        "WHERE memories_fts MATCH ? AND m.archived_ts IS NULL "
        "ORDER BY rank LIMIT ?",
        (fts, limit),
    ).fetchall()
    return [Memory.from_row(r) for r in rows]


def list_memories(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = False,
    order: str = "created",
) -> list[Memory]:
    """All memories, optionally including archived.

    order:
      - "created"   → newest first (created_ts DESC)
      - "audit"     → never-verified first, then oldest-verified (consolidation)
      - "dashboard" → active first then archived, newest id first within each
    """
    where = "" if include_archived else "WHERE archived_ts IS NULL"
    if order == "audit":
        order_sql = "ORDER BY (last_verified_ts IS NOT NULL), last_verified_ts, created_ts"
    elif order == "dashboard":
        order_sql = "ORDER BY archived_ts IS NOT NULL, id DESC"
    else:
        order_sql = "ORDER BY created_ts DESC"
    rows = conn.execute(f"SELECT {_MEM_COLS} FROM memories {where} {order_sql}").fetchall()
    return [Memory.from_row(r) for r in rows]


def name_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if a memory with this exact name exists (any archive state)."""
    return conn.execute(
        "SELECT 1 FROM memories WHERE name = ? LIMIT 1", (name,)
    ).fetchone() is not None


def all_triggers(conn: sqlite3.Connection) -> list[Trigger]:
    """Every trigger, ordered by memory_id (dashboard rendering)."""
    rows = conn.execute(
        "SELECT id, memory_id, kind, first_token, tokens_json, path_pattern "
        "FROM triggers ORDER BY memory_id"
    ).fetchall()
    return [Trigger.from_row(r) for r in rows]


def count_token_trigger_owners(conn: sqlite3.Connection, tokens: list[str]) -> int:
    """How many memories have a token_subseq trigger with exactly these tokens."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT memory_id) FROM triggers "
        "WHERE kind = 'token_subseq' AND tokens_json = ?",
        (json.dumps(list(tokens)),),
    ).fetchone()
    return int(row[0] or 0)


def count_path_trigger_owners(conn: sqlite3.Connection, path_pattern: str) -> int:
    """How many memories have a path_glob trigger with exactly this pattern."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT memory_id) FROM triggers "
        "WHERE kind = 'path_glob' AND path_pattern = ?",
        (path_pattern,),
    ).fetchone()
    return int(row[0] or 0)


def count_active(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE archived_ts IS NULL"
    ).fetchone()
    return row["c"] if row else 0


def summary_stats(conn: sqlite3.Connection) -> dict:
    """Aggregate counts for `engram recall --stats`: totals, by-kind, by-scope,
    and triggers-by-kind (active memories only for the breakdowns)."""
    kind_counts = conn.execute(
        "SELECT kind, COUNT(*) AS c FROM memories "
        "WHERE archived_ts IS NULL GROUP BY kind"
    ).fetchall()
    scope_counts = conn.execute(
        "SELECT scope, COUNT(*) AS c FROM memories "
        "WHERE archived_ts IS NULL GROUP BY scope"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) AS pinned, "
        "SUM(CASE WHEN archived_ts IS NOT NULL THEN 1 ELSE 0 END) AS archived "
        "FROM memories"
    ).fetchone()
    trigger_counts = conn.execute(
        "SELECT t.kind AS kind, COUNT(*) AS c FROM triggers t "
        "JOIN memories m ON t.memory_id = m.id "
        "WHERE m.archived_ts IS NULL GROUP BY t.kind"
    ).fetchall()
    return {
        "total": total["total"],
        "active": total["total"] - (total["archived"] or 0),
        "pinned": total["pinned"] or 0,
        "archived": total["archived"] or 0,
        "by_kind": {r["kind"]: r["c"] for r in kind_counts},
        "by_scope": {r["scope"]: r["c"] for r in scope_counts},
        "triggers_by_kind": {r["kind"]: r["c"] for r in trigger_counts},
    }


def health_stats(conn: sqlite3.Connection) -> dict:
    """Memory health aggregate for `engram status`: active/archived counts and
    the surface/useful totals over active memories, plus triggers-by-kind."""
    m = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN archived_ts IS NULL THEN 1 ELSE 0 END) AS active, "
        "  SUM(CASE WHEN archived_ts IS NOT NULL THEN 1 ELSE 0 END) AS archived, "
        "  SUM(CASE WHEN archived_ts IS NULL THEN surface_count ELSE 0 END) AS total_surfaces, "
        "  SUM(CASE WHEN archived_ts IS NULL THEN useful_count ELSE 0 END) AS total_useful "
        "FROM memories"
    ).fetchone()
    triggers = conn.execute(
        "SELECT t.kind AS kind, COUNT(*) AS c FROM triggers t "
        "JOIN memories m ON t.memory_id = m.id WHERE m.archived_ts IS NULL GROUP BY t.kind"
    ).fetchall()
    return {
        "active": m["active"] or 0,
        "archived": m["archived"] or 0,
        "total_surfaces": m["total_surfaces"] or 0,
        "total_useful": m["total_useful"] or 0,
        "triggers_by_kind": {r["kind"]: r["c"] for r in triggers},
    }


def triggers_for(conn: sqlite3.Connection, memory_id: int) -> list[Trigger]:
    rows = conn.execute(
        "SELECT id, memory_id, kind, first_token, tokens_json, path_pattern "
        "FROM triggers WHERE memory_id = ?",
        (memory_id,),
    ).fetchall()
    return [Trigger.from_row(r) for r in rows]


# ---------- hot-path match (raw rows, no Memory allocation) ----------


def match_token_triggers(conn: sqlite3.Connection, first_token: str,
                         project_slug: str | None, kind: str | None) -> list[sqlite3.Row]:
    """token_subseq candidates for a first_token, scope-filtered, non-archived.

    Returns raw rows (m.* subset + t.tokens_json) — rank.py subsequence-matches
    and builds Candidates. Lean on purpose: this runs on every tool call.
    """
    kind_sql = " AND m.kind = ?" if kind else ""
    args = (first_token, project_slug, kind) if kind else (first_token, project_slug)
    return conn.execute(
        "SELECT m.id, m.name, m.body, m.kind, m.scope, m.surface_count, "
        "       m.useful_count, m.noise_count, m.last_surfaced_ts, m.pinned, t.tokens_json "
        "FROM triggers t JOIN memories m ON m.id = t.memory_id "
        "WHERE t.kind = 'token_subseq' AND t.first_token = ? "
        "  AND m.archived_ts IS NULL "
        "  AND (m.scope = 'global' OR m.project_slug = ?)"
        f"{kind_sql}",
        args,
    ).fetchall()


def match_path_triggers(conn: sqlite3.Connection, project_slug: str | None,
                        kind: str | None) -> list[sqlite3.Row]:
    """path_glob candidates, scope-filtered, non-archived (raw rows for rank.py)."""
    kind_sql = " AND m.kind = ?" if kind else ""
    args = (project_slug, kind) if kind else (project_slug,)
    return conn.execute(
        "SELECT m.id, m.name, m.body, m.kind, m.scope, m.surface_count, "
        "       m.useful_count, m.noise_count, m.last_surfaced_ts, m.pinned, t.path_pattern "
        "FROM triggers t JOIN memories m ON m.id = t.memory_id "
        "WHERE t.kind = 'path_glob' AND m.archived_ts IS NULL "
        "  AND (m.scope = 'global' OR m.project_slug = ?)"
        f"{kind_sql}",
        args,
    ).fetchall()


def overlap_rows(conn: sqlite3.Connection, project_slug: str | None) -> list[sqlite3.Row]:
    """All (memory, trigger) pairs in scope, non-archived — for dedup overlap
    scoring. Returns raw rows (m.id, m.name, t.kind, t.tokens_json, t.path_pattern)."""
    return conn.execute(
        "SELECT m.id, m.name, t.kind, t.tokens_json, t.path_pattern "
        "FROM memories m JOIN triggers t ON t.memory_id = m.id "
        "WHERE m.archived_ts IS NULL "
        "  AND (m.scope = 'global' OR m.project_slug = ?)",
        (project_slug,),
    ).fetchall()


# ---------- memory writes ----------


def insert_memory(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str | None,
    body: str,
    kind: str,
    scope: str,
    project_slug: str | None,
    pinned: bool,
    created_ts: int,
) -> int:
    """Insert a memory row, return its id."""
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, description, body, kind, scope, project_slug, pinned, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, description, body, kind, scope, project_slug, 1 if pinned else 0, created_ts),
    )
    return int(cur.lastrowid)


def update_memory(
    conn: sqlite3.Connection,
    memory_id: int,
    *,
    name: str,
    description: str | None,
    body: str,
    kind: str,
    pinned: bool,
    created_ts: int,
) -> None:
    """Replace the editable fields of an existing memory (dedup update path)."""
    conn.execute(
        "UPDATE memories SET name = ?, description = ?, body = ?, kind = ?, "
        "pinned = ?, created_ts = ? WHERE id = ?",
        (name, description, body, kind, 1 if pinned else 0, created_ts, memory_id),
    )


def set_kind(conn: sqlite3.Connection, memory_id: int, kind: str) -> None:
    """Change only a memory's kind (seed realigns legacy demo memories)."""
    conn.execute(
        "UPDATE memories SET kind = ? WHERE id = ?",
        (kind, memory_id),
    )


def set_pinned(conn: sqlite3.Connection, memory_id: int, pinned: bool) -> None:
    conn.execute(
        "UPDATE memories SET pinned = ? WHERE id = ?",
        (1 if pinned else 0, memory_id),
    )


def set_verified(conn: sqlite3.Connection, memory_id: int, verified_ts: int) -> None:
    conn.execute(
        "UPDATE memories SET last_verified_ts = ? WHERE id = ?",
        (verified_ts, memory_id),
    )


def delete_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    """Hard-delete a memory and its triggers (FK ON DELETE CASCADE handles the
    triggers, but we delete explicitly so it works regardless of pragma)."""
    conn.execute("DELETE FROM triggers WHERE memory_id = ?", (memory_id,))
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))


# ---------- reinforcement counter bumps ----------


def bump_surface(conn: sqlite3.Connection, memory_ids: Sequence[int], now_ts: int) -> None:
    """Increment surface_count and refresh last_surfaced_ts for each memory."""
    if not memory_ids:
        return
    placeholders = ",".join("?" * len(memory_ids))
    conn.execute(
        f"UPDATE memories SET surface_count = surface_count + 1, "
        f"last_surfaced_ts = ? WHERE id IN ({placeholders})",
        (now_ts, *memory_ids),
    )


def bump_useful(conn: sqlite3.Connection, memory_ids: Sequence[int]) -> None:
    """Increment useful_count for each memory — a 'helpful' verdict."""
    if not memory_ids:
        return
    placeholders = ",".join("?" * len(memory_ids))
    conn.execute(
        f"UPDATE memories SET useful_count = useful_count + 1 "
        f"WHERE id IN ({placeholders})",
        list(memory_ids),
    )


def bump_noise(conn: sqlite3.Connection, memory_ids: Sequence[int]) -> None:
    """Increment noise_count for each memory — a 'noise' verdict (the trigger
    over-matched). Paired with bump_useful, this feeds the q quality ratio."""
    if not memory_ids:
        return
    placeholders = ",".join("?" * len(memory_ids))
    conn.execute(
        f"UPDATE memories SET noise_count = noise_count + 1 "
        f"WHERE id IN ({placeholders})",
        list(memory_ids),
    )


def soft_demote(conn: sqlite3.Connection, memory_id: int) -> None:
    """Crater usefulness without archiving: useful=0, surface_count += penalty."""
    conn.execute(
        "UPDATE memories SET useful_count = 0, "
        "surface_count = surface_count + ?, last_surfaced_ts = 0 WHERE id = ?",
        (SOFT_DEMOTE_PENALTY, memory_id),
    )


def archive(conn: sqlite3.Connection, memory_id: int, now_ts: int | None = None) -> None:
    """Mark a memory archived. Excluded from retrieval."""
    ts = now_ts if now_ts is not None else int(time.time())
    conn.execute("UPDATE memories SET archived_ts = ? WHERE id = ?", (ts, memory_id))


def restore(conn: sqlite3.Connection, memory_id: int) -> None:
    """Undo a soft-demote or archive: clear archive and reset counters to zero."""
    conn.execute(
        "UPDATE memories SET archived_ts = NULL, "
        "useful_count = 0, surface_count = 0, last_surfaced_ts = 0 WHERE id = ?",
        (memory_id,),
    )


# ---------- trigger writes ----------


def add_token_trigger(conn: sqlite3.Connection, memory_id: int,
                      first_token: str, tokens: list[str]) -> None:
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', ?, ?)",
        (memory_id, first_token, json.dumps(list(tokens))),
    )


def add_path_trigger(conn: sqlite3.Connection, memory_id: int, path_pattern: str) -> None:
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) VALUES (?, 'path_glob', ?)",
        (memory_id, path_pattern),
    )


def delete_triggers_for(conn: sqlite3.Connection, memory_id: int) -> None:
    conn.execute("DELETE FROM triggers WHERE memory_id = ?", (memory_id,))


def delete_trigger(conn: sqlite3.Connection, trigger_id: int) -> None:
    conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))


def count_triggers_for(conn: sqlite3.Connection, memory_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM triggers WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    return int(row["c"] if row else 0)


# ---------- maintenance reads (engram rebuild-triggers) ----------


def list_triggerless(conn: sqlite3.Connection) -> list[Memory]:
    """Active memories that have no triggers at all."""
    rows = conn.execute(
        f"SELECT {_cols('m')} FROM memories m "
        "LEFT JOIN triggers t ON t.memory_id = m.id "
        "WHERE m.archived_ts IS NULL AND t.id IS NULL "
        "GROUP BY m.id"
    ).fetchall()
    return [Memory.from_row(r) for r in rows]


def list_active_token_triggers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """token_subseq triggers on active memories, with the owning memory name —
    raw rows for the malformed-trigger audit (t.id, t.memory_id, t.first_token,
    t.tokens_json, m.name)."""
    return conn.execute(
        "SELECT t.id, t.memory_id, t.first_token, t.tokens_json, m.name "
        "FROM triggers t JOIN memories m ON m.id = t.memory_id "
        "WHERE t.kind = 'token_subseq' AND m.archived_ts IS NULL"
    ).fetchall()
