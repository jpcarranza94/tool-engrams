-- Migration v1 → v2: Hebbian associations + consolidation runs
-- Applied by db._migrate() when user_version < 2.

-- Hebbian co-activation: symmetric memory pair associations.
-- Canonical ordering: memory_a_id < memory_b_id always.
CREATE TABLE IF NOT EXISTS memory_associations (
    memory_a_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    memory_b_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    strength        REAL NOT NULL DEFAULT 0.0,
    co_fire_count   INTEGER NOT NULL DEFAULT 0,
    last_co_fire_ts INTEGER NOT NULL DEFAULT 0,
    created_ts      INTEGER NOT NULL,
    PRIMARY KEY (memory_a_id, memory_b_id),
    CHECK (memory_a_id < memory_b_id)
);

-- Both directions need indexed lookups for the boost query.
CREATE INDEX IF NOT EXISTS idx_assoc_b_a
    ON memory_associations(memory_b_id, memory_a_id);

-- Nightly consolidation run log — idempotency guard.
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL UNIQUE,       -- 'YYYY-MM-DD'
    started_ts      INTEGER NOT NULL,
    completed_ts    INTEGER,
    sessions_scanned INTEGER NOT NULL DEFAULT 0,
    episodes_evaluated INTEGER NOT NULL DEFAULT 0,
    memories_strengthened INTEGER NOT NULL DEFAULT 0,
    memories_weakened INTEGER NOT NULL DEFAULT 0,
    memories_archived INTEGER NOT NULL DEFAULT 0,
    memories_discovered INTEGER NOT NULL DEFAULT 0,
    report          TEXT                         -- structured text summary
);

-- Clean up triggers CHECK constraint (remove dead kinds from v1).
-- SQLite doesn't support ALTER TABLE ... ALTER COLUMN, so we leave the
-- existing CHECK in place. New rows only use tool_head|path_glob anyway.
-- The dead columns (error_substring, keyword) are harmless nulls.

-- Session surfaces TTL index for cleanup queries.
CREATE INDEX IF NOT EXISTS idx_session_surfaces_ts
    ON session_surfaces(surfaced_ts);
