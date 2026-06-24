-- ToolEngrams schema — complete current snapshot. Fresh DBs apply this ONLY (not the
-- v*.sql migrations, which are for upgrading existing DBs).

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    description      TEXT,
    body             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('block','hint')),
    scope            TEXT NOT NULL CHECK (scope IN ('global','project')) DEFAULT 'project',
    project_slug     TEXT,
    created_ts       INTEGER NOT NULL,
    last_surfaced_ts INTEGER NOT NULL DEFAULT 0,
    surface_count    INTEGER NOT NULL DEFAULT 0,
    useful_count     INTEGER NOT NULL DEFAULT 0,   -- helpful verdicts (eval watcher)
    noise_count      INTEGER NOT NULL DEFAULT 0,   -- noise verdicts (trigger over-matched)
    pinned           INTEGER NOT NULL DEFAULT 0,
    archived_ts      INTEGER,
    last_verified_ts INTEGER DEFAULT NULL,
    origin_session_id TEXT                -- work session that formed it; NULL for
                                          -- manual saves. Same-session hint
                                          -- suppression keys on it (ADR-0006).
);

CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope, project_slug, archived_ts);

CREATE TABLE IF NOT EXISTS triggers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('token_subseq','path_glob')),
    first_token     TEXT,     -- tokens[0] stored as-is (lookup is case-sensitive,
                              -- like command names); null for path_glob
    tokens_json     TEXT,     -- JSON array of required tokens in order; null for path_glob
    path_pattern    TEXT,     -- fnmatch pattern; null for token_subseq
    access_mode     TEXT      -- path_glob access intent: 'write'|'read'|'any'.
                              -- 'write' fires only on Edit/Write/MultiEdit/
                              -- NotebookEdit; 'read' only on Read/Grep/Glob;
                              -- 'any' on either. NULL for token_subseq (and
                              -- legacy path rows = match-any, fail-open).
);

CREATE INDEX IF NOT EXISTS idx_triggers_first_token
    ON triggers(first_token)
    WHERE kind = 'token_subseq';

CREATE INDEX IF NOT EXISTS idx_triggers_memory
    ON triggers(memory_id);

CREATE TABLE IF NOT EXISTS session_surfaces (
    session_id       TEXT NOT NULL,
    memory_id        INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    surfaced_ts      INTEGER NOT NULL,
    hook             TEXT NOT NULL,
    tool_use_id      TEXT,
    turn_at_surface  INTEGER,
    first_token      TEXT,
    outcome          TEXT
        CHECK (outcome IS NULL OR outcome IN ('helpful', 'unused', 'noise'))
        DEFAULT NULL,
    PRIMARY KEY (session_id, memory_id, surfaced_ts)
);

CREATE INDEX IF NOT EXISTS idx_session_surfaces_recent
    ON session_surfaces(session_id, surfaced_ts DESC);

CREATE INDEX IF NOT EXISTS idx_session_surfaces_ts
    ON session_surfaces(surfaced_ts);

CREATE INDEX IF NOT EXISTS idx_session_surfaces_turn
    ON session_surfaces(session_id, turn_at_surface);

CREATE INDEX IF NOT EXISTS idx_session_surfaces_failure_token
    ON session_surfaces(session_id, hook, first_token)
    WHERE hook = 'post_tool_use_failure' AND outcome IS NULL;

-- Per-session tool-call counter.
CREATE TABLE IF NOT EXISTS session_turns (
    session_id TEXT PRIMARY KEY,
    turn_count INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL
);

-- Nightly consolidation run log — idempotency guard + health metrics.
CREATE TABLE IF NOT EXISTS consolidation_runs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date               TEXT NOT NULL UNIQUE,       -- 'YYYY-MM-DD'
    started_ts             INTEGER NOT NULL,
    completed_ts           INTEGER,
    sessions_scanned       INTEGER NOT NULL DEFAULT 0,
    episodes_evaluated     INTEGER NOT NULL DEFAULT 0,
    memories_strengthened  INTEGER NOT NULL DEFAULT 0,
    memories_weakened      INTEGER NOT NULL DEFAULT 0,
    memories_archived      INTEGER NOT NULL DEFAULT 0,
    memories_discovered    INTEGER NOT NULL DEFAULT 0,
    quality_score          REAL,
    surfaces_helpful       INTEGER NOT NULL DEFAULT 0,
    surfaces_noise         INTEGER NOT NULL DEFAULT 0,
    memories_verified      INTEGER NOT NULL DEFAULT 0,
    report                 TEXT
);

-- Watcher state — two symmetric rows per work session, one per role
-- (formation | eval). Tracks each role's own transcript cursor and the
-- event-driven tick state (armed / coalesce / cross-event retry streak).
CREATE TABLE IF NOT EXISTS watcher_state (
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
    target             TEXT NOT NULL DEFAULT 'claude-code',  -- which harness's transcript format to parse
    PRIMARY KEY (work_session_id, role)
);

-- Live-monitor run log: one row per model-calling tick, read back by
-- `engram monitor`. Ticks that gate out write nothing.
CREATE TABLE IF NOT EXISTS watcher_runs (
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
    cost_usd         REAL,                                 -- from the claude -p JSON envelope
    input_tokens     INTEGER,                              -- (all NULL when the call errored
    output_tokens    INTEGER,                              --  or predates v14)
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    engine           TEXT                                   -- engine adapter that ran the call (NULL predates v16)
);

CREATE INDEX IF NOT EXISTS idx_watcher_runs_recent
    ON watcher_runs(started_ts DESC);
CREATE INDEX IF NOT EXISTS idx_watcher_runs_session
    ON watcher_runs(work_session_id, role, status);

-- One row per memory a run created/judged (via the engram CLI, tied by
-- $ENGRAM_RUN_ID) — the decision stream.
CREATE TABLE IF NOT EXISTS watcher_run_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES watcher_runs(id) ON DELETE CASCADE,
    ts           INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('created','judged','quarantined')),
    memory_id    INTEGER,
    memory_name  TEXT,
    outcome      TEXT CHECK (outcome IN ('helpful','unused','noise') OR outcome IS NULL),
    detail       TEXT                                      -- quarantine reason
);

CREATE INDEX IF NOT EXISTS idx_watcher_run_events_run
    ON watcher_run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_watcher_run_events_ts
    ON watcher_run_events(ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    name, description, body,
    content='memories',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, name, description, body)
    VALUES (new.id, new.name, new.description, new.body);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
    VALUES ('delete', old.id, old.name, old.description, old.body);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
    VALUES ('delete', old.id, old.name, old.description, old.body);
    INSERT INTO memories_fts(rowid, name, description, body)
    VALUES (new.id, new.name, new.description, new.body);
END;
