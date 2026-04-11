-- ToolEngrams schema v1
-- Canonical store. See docs/design-v8.md for full design.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    description      TEXT,
    body             TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('user','feedback','project','reference')),
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
    kind            TEXT NOT NULL CHECK (kind IN ('tool_head','path_glob','error_contains','keyword')),
    tool_name       TEXT,
    head_joined     TEXT,
    head_length     INTEGER,
    path_pattern    TEXT,
    error_substring TEXT,
    keyword         TEXT
);

CREATE INDEX IF NOT EXISTS idx_triggers_tool_head
    ON triggers(tool_name, head_joined)
    WHERE kind = 'tool_head';

CREATE INDEX IF NOT EXISTS idx_triggers_error
    ON triggers(tool_name)
    WHERE kind = 'error_contains';

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
