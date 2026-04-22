-- ToolEngrams schema (current v2). See docs/design-v9.md for full design.
-- Incremental reshape for existing DBs lives in migrations/v*.sql.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    description      TEXT,
    body             TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('feedback','reference')),
    scope            TEXT NOT NULL CHECK (scope IN ('global','project')) DEFAULT 'project',
    project_slug     TEXT,
    created_ts       INTEGER NOT NULL,
    last_surfaced_ts INTEGER NOT NULL DEFAULT 0,
    surface_count    INTEGER NOT NULL DEFAULT 0,
    useful_count     INTEGER NOT NULL DEFAULT 0,
    pinned           INTEGER NOT NULL DEFAULT 0,
    archived_ts      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope, project_slug, archived_ts);

CREATE TABLE IF NOT EXISTS triggers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('token_subseq','path_glob')),
    first_token     TEXT,     -- lowercased for indexed lookup; null for path_glob
    tokens_json     TEXT,     -- JSON array of required tokens in order; null for path_glob
    path_pattern    TEXT      -- fnmatch pattern; null for token_subseq
);

CREATE INDEX IF NOT EXISTS idx_triggers_first_token
    ON triggers(first_token)
    WHERE kind = 'token_subseq';

CREATE INDEX IF NOT EXISTS idx_triggers_memory
    ON triggers(memory_id);

CREATE TABLE IF NOT EXISTS session_surfaces (
    session_id   TEXT NOT NULL,
    memory_id    INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    surfaced_ts  INTEGER NOT NULL,
    hook         TEXT NOT NULL,
    tool_use_id  TEXT,
    PRIMARY KEY (session_id, memory_id, surfaced_ts)
);

CREATE INDEX IF NOT EXISTS idx_session_surfaces_recent
    ON session_surfaces(session_id, surfaced_ts DESC);

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
