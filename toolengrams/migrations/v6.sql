-- Migration v5 → v6: v2 design - required-token subsequence triggers.
--
-- Replaces v1's (tool_name, head_joined, head_length) prefix-match trigger
-- shape with (first_token, tokens_json) subsequence-match. Handles the
-- `ergeon order <id> reassign` positional-ID-between-verbs case that v1
-- prefix matching couldn't hit.
--
-- Alpha stage, no users: we drop and recreate the triggers table. A
-- best-effort re-population is handled separately by `engram migrate-v1-to-v2`
-- (shipped later as part of v2 step 6).
--
-- Also drop Hebbian associations — recall itself isn't reliable yet; maintaining
-- a secondary ranking signal is premature (design-v9 §12).
--
-- memories.type stays `feedback|reference` for now — the rename to `kind`
-- (block|hint) lands in v2 step 2.

DROP INDEX IF EXISTS idx_triggers_tool_head;
DROP INDEX IF EXISTS idx_triggers_error;
DROP INDEX IF EXISTS idx_triggers_memory;
DROP TABLE IF EXISTS triggers;

DROP INDEX IF EXISTS idx_assoc_b_a;
DROP TABLE IF EXISTS memory_associations;

CREATE TABLE triggers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('token_subseq','path_glob')),
    first_token     TEXT,     -- lowercased for indexed lookup; null for path_glob
    tokens_json     TEXT,     -- JSON array of required tokens in order; null for path_glob
    path_pattern    TEXT      -- fnmatch pattern; null for token_subseq
);

CREATE INDEX idx_triggers_first_token
    ON triggers(first_token)
    WHERE kind = 'token_subseq';

CREATE INDEX idx_triggers_memory
    ON triggers(memory_id);
