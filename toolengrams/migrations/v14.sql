-- Migration v13 → v14: per-run cost + token usage.
--
-- The watcher's `claude -p --output-format json` envelope reports the call's
-- exact cost (total_cost_usd) and token usage; until now both were discarded
-- after the session_id was extracted. Capture them on the run row so
-- `engram monitor` can show what the background watchers actually spend.
--
-- Rebuild instead of ALTER ADD COLUMN: the forward chain must be idempotent
-- against the schema.sql snapshot (see v13), and SQLite has no ADD COLUMN IF
-- NOT EXISTS. The copy lists only pre-v14 columns, so it works whether or not
-- the snapshot already carries the new ones. All new columns are NULL for
-- error/timeout runs (no envelope) and for rows from before this migration.

-- Keep RENAME from rewriting watcher_run_events' FK text to the tmp name:
-- with foreign_keys ON, RENAME updates REFERENCES clauses regardless of
-- legacy_alter_table — both must be set for the rebuild. Pragmas sit OUTSIDE
-- the transaction (foreign_keys is a no-op inside one).
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

-- The connection is autocommit, and this is the repo's first DESTRUCTIVE
-- migration (v2–v13 are additive CREATE IF NOT EXISTS). Without a transaction,
-- a crash between the RENAME and the CREATE strands the DB with no
-- watcher_runs and user_version still 13 — every later open re-runs the
-- script and fails on the RENAME, forever. The transaction makes a crash roll
-- back to intact v13; the re-run is the verified double-apply path.
BEGIN;

ALTER TABLE watcher_runs RENAME TO watcher_runs_v13_tmp;

CREATE TABLE watcher_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    work_session_id  TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('formation','eval')),
    status           TEXT NOT NULL CHECK (status IN ('running','ok','error','crashed'))
                       DEFAULT 'running',
    pid              INTEGER,
    started_ts       INTEGER NOT NULL,
    ended_ts         INTEGER,
    model            TEXT,
    flush            INTEGER NOT NULL DEFAULT 0,
    cursor_from      INTEGER,
    cursor_to        INTEGER,
    delta_chars      INTEGER,
    cwd              TEXT,
    error            TEXT,
    cost_usd         REAL,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER
);

INSERT INTO watcher_runs
    (id, work_session_id, role, status, pid, started_ts, ended_ts, model,
     flush, cursor_from, cursor_to, delta_chars, cwd, error)
    SELECT id, work_session_id, role, status, pid, started_ts, ended_ts, model,
           flush, cursor_from, cursor_to, delta_chars, cwd, error
    FROM watcher_runs_v13_tmp;

DROP TABLE watcher_runs_v13_tmp;

CREATE INDEX IF NOT EXISTS idx_watcher_runs_recent
    ON watcher_runs(started_ts DESC);
CREATE INDEX IF NOT EXISTS idx_watcher_runs_session
    ON watcher_runs(work_session_id, role, status);

COMMIT;

PRAGMA legacy_alter_table = OFF;
-- Restore the process default. The app never enables FK enforcement (see
-- runs_store.prune_runs_before, which deletes events manually for exactly
-- that reason) — ending with ON would leave the one migrating connection
-- enforcing constraints nothing else does.
PRAGMA foreign_keys = OFF;
