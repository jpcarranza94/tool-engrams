-- Migration v6 → v7: memories.type → memories.kind (feedback/reference → block/hint).
--
-- See docs/design-v9.md §3.1 and §10 step 2. Two memory kinds, one trigger
-- mechanism, two surface moments:
--   block: PreToolUse denies the call, injects body as context (rare)
--   hint:  PostToolUseFailure injects body as context (default)
--
-- Value map (best-effort, alpha stage):
--   feedback  → block  (PreToolUse-deny behavior preserved)
--   reference → hint   (moves from pre-call context to post-failure context)
--
-- SQLite doesn't support ALTER TABLE ... ALTER COLUMN for CHECK constraints,
-- so we rebuild the memories table. FTS and its triggers must be rebuilt too
-- because the content table is being dropped and recreated.

-- Drop FTS shadow tables + triggers before dropping memories (they reference it).
DROP TRIGGER IF EXISTS memories_ai;
DROP TRIGGER IF EXISTS memories_ad;
DROP TRIGGER IF EXISTS memories_au;
DROP TABLE IF EXISTS memories_fts;

CREATE TABLE memories_new (
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
    useful_count     INTEGER NOT NULL DEFAULT 0,
    pinned           INTEGER NOT NULL DEFAULT 0,
    archived_ts      INTEGER
);

INSERT INTO memories_new (
    id, name, description, body, kind, scope, project_slug,
    created_ts, last_surfaced_ts, surface_count, useful_count, pinned, archived_ts
)
SELECT
    id, name, description, body,
    CASE type
        WHEN 'feedback'  THEN 'block'
        WHEN 'reference' THEN 'hint'
        ELSE 'hint'
    END AS kind,
    scope, project_slug,
    created_ts, last_surfaced_ts, surface_count, useful_count, pinned, archived_ts
FROM memories;

DROP INDEX IF EXISTS idx_memories_scope;
DROP TABLE memories;
ALTER TABLE memories_new RENAME TO memories;

CREATE INDEX idx_memories_scope
    ON memories(scope, project_slug, archived_ts);

-- Rebuild FTS + triggers on the new table.
CREATE VIRTUAL TABLE memories_fts USING fts5(
    name, description, body,
    content='memories',
    content_rowid='id'
);

INSERT INTO memories_fts(rowid, name, description, body)
SELECT id, name, description, body FROM memories;

CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, name, description, body)
    VALUES (new.id, new.name, new.description, new.body);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
    VALUES ('delete', old.id, old.name, old.description, old.body);
END;

CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, body)
    VALUES ('delete', old.id, old.name, old.description, old.body);
    INSERT INTO memories_fts(rowid, name, description, body)
    VALUES (new.id, new.name, new.description, new.body);
END;
