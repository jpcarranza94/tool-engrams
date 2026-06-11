-- v15 — stateless watcher + signal integrity (ADR-0005, ADR-0006, ADR-0007).
--
-- 1. memories.origin_session_id: which work session formed the memory (NULL for
--    manual saves). Same-session hint suppression keys on it (ADR-0006).
-- 2. watcher_run_events: kind CHECK gains 'quarantined' and a `detail` column
--    carries the quarantine reason (ADR-0007). SQLite CHECK changes need a
--    table rebuild.
-- 3. watcher_state: drop watcher_session_id + watcher_pid — resume is gone
--    (ADR-0005) and the pid column was never written. Table rebuild.

ALTER TABLE memories ADD COLUMN origin_session_id TEXT;

CREATE TABLE watcher_run_events_v15 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES watcher_runs(id) ON DELETE CASCADE,
    ts           INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('created','judged','quarantined')),
    memory_id    INTEGER,
    memory_name  TEXT,
    outcome      TEXT CHECK (outcome IN ('helpful','unused','noise') OR outcome IS NULL),
    detail       TEXT                                      -- quarantine reason
);
INSERT INTO watcher_run_events_v15
    (id, run_id, ts, kind, memory_id, memory_name, outcome)
    SELECT id, run_id, ts, kind, memory_id, memory_name, outcome
    FROM watcher_run_events;
DROP TABLE watcher_run_events;
ALTER TABLE watcher_run_events_v15 RENAME TO watcher_run_events;
CREATE INDEX IF NOT EXISTS idx_watcher_run_events_run
    ON watcher_run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_watcher_run_events_ts
    ON watcher_run_events(ts DESC);

CREATE TABLE watcher_state_v15 (
    work_session_id    TEXT NOT NULL,
    role               TEXT NOT NULL DEFAULT 'formation'
                         CHECK (role IN ('formation','eval')),
    transcript_path    TEXT,
    last_line_read     INTEGER NOT NULL DEFAULT 0,
    last_checked_ts    INTEGER NOT NULL,
    cwd                TEXT,
    created_ts         INTEGER NOT NULL,
    armed              INTEGER NOT NULL DEFAULT 0,
    last_tick_ts       INTEGER NOT NULL DEFAULT 0,
    fail_streak        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (work_session_id, role)
);
INSERT INTO watcher_state_v15
    (work_session_id, role, transcript_path, last_line_read, last_checked_ts,
     cwd, created_ts, armed, last_tick_ts, fail_streak)
    SELECT work_session_id, role, transcript_path, last_line_read,
           last_checked_ts, cwd, created_ts, armed, last_tick_ts, fail_streak
    FROM watcher_state;
DROP TABLE watcher_state;
ALTER TABLE watcher_state_v15 RENAME TO watcher_state;
