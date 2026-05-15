-- Migration v4 → v5: Watcher state table.
-- Tracks the persistent parallel watcher (background `claude -p`) session
-- per work session. Model is configured via $ENGRAM_WATCHER_MODEL.

CREATE TABLE IF NOT EXISTS watcher_state (
    work_session_id    TEXT PRIMARY KEY,
    watcher_session_id TEXT,
    watcher_pid        INTEGER,
    transcript_path    TEXT,
    last_line_read     INTEGER NOT NULL DEFAULT 0,
    last_checked_ts    INTEGER NOT NULL,
    cwd                TEXT,
    created_ts         INTEGER NOT NULL
);
