-- Migration v12 → v13: the live-monitor run log.
--
-- `engram monitor` is a live dashboard over the watchers, but ticks are
-- detached and sub-minute, so there is no process to inspect. Instead each
-- model-calling tick records a `watcher_runs` row, and every memory the run
-- creates/judges (via the engram CLI, tied back by $ENGRAM_RUN_ID) records a
-- `watcher_run_events` row — the dashboard reads these back.
--
-- CREATE … IF NOT EXISTS keeps the forward chain idempotent against the schema.sql
-- snapshot, so the older migration tests need no changes.

-- One row per model-calling tick (formation or eval). Ticks that gate out
-- (chat turn, no new lines, eval defer, coalesced, lock-contended) write nothing.
CREATE TABLE IF NOT EXISTS watcher_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,   -- the run id ($ENGRAM_RUN_ID)
    work_session_id  TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('formation','eval')),
    status           TEXT NOT NULL CHECK (status IN ('running','ok','error','crashed'))
                       DEFAULT 'running',
    pid              INTEGER,                              -- for live-vs-stale display
    started_ts       INTEGER NOT NULL,
    ended_ts         INTEGER,                              -- NULL while running/crashed
    model            TEXT,                                 -- e.g. 'opus'
    flush            INTEGER NOT NULL DEFAULT 0,
    cursor_from      INTEGER,                              -- transcript line span read
    cursor_to        INTEGER,
    delta_chars      INTEGER,                              -- window size (big-delta signal)
    cwd              TEXT,                                 -- denormalized: project, immutable
    error            TEXT                                  -- short reason when error/crashed
);

CREATE INDEX IF NOT EXISTS idx_watcher_runs_recent
    ON watcher_runs(started_ts DESC);
CREATE INDEX IF NOT EXISTS idx_watcher_runs_session
    ON watcher_runs(work_session_id, role, status);

-- One row per CLI action a run produced (the decision stream).
CREATE TABLE IF NOT EXISTS watcher_run_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES watcher_runs(id) ON DELETE CASCADE,
    ts           INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('created','judged')),  -- formation | eval
    memory_id    INTEGER,                                 -- no hard FK: survives deletion
    memory_name  TEXT,                                    -- denormalized: reads if archived
    outcome      TEXT CHECK (outcome IN ('helpful','unused','noise') OR outcome IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_watcher_run_events_run
    ON watcher_run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_watcher_run_events_ts
    ON watcher_run_events(ts DESC);
