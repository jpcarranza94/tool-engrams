-- Migration v8 → v9: outcome tracking on session_surfaces.
--
-- Closes two reinforcement gaps:
--   (1) Post_tool_failure hints never accumulate useful_count — the current
--       PostToolUse bump only looks at hook='pre_tool_use' surfaces. Now a
--       successful tool call also credits failure-surfaces from earlier in
--       the same session whose first_token matches the current call. The
--       schema gains session_surfaces.first_token so the lookup is cheap.
--   (2) There's no negative signal. Today the only way to mark a memory
--       "not useful" is for the nightly consolidation agent to soft-demote
--       it. Now Claude (or the agent) can call `engram skip <name>` to
--       mark the most recent surface 'unused', and the consolidation agent
--       can mark 'noise' retrospectively. outcome=NULL keeps "no judgment
--       yet" as the default.
--
-- Both columns are nullable so older session_surfaces rows stay valid.
-- The first_token column is populated going forward by log_surfaces();
-- a best-effort backfill matches existing rows against the triggers
-- table — older rows without a recoverable first_token stay NULL.

ALTER TABLE session_surfaces ADD COLUMN first_token TEXT;
ALTER TABLE session_surfaces ADD COLUMN outcome TEXT
    CHECK (outcome IS NULL OR outcome IN ('helpful', 'unused', 'noise'))
    DEFAULT NULL;

-- Best-effort backfill: pick any token_subseq trigger for the memory and
-- copy its first_token into pre-existing surface rows. Memories with only
-- path_glob triggers stay NULL — there's no useful first_token for them.
UPDATE session_surfaces
SET first_token = (
    SELECT t.first_token
    FROM triggers t
    WHERE t.memory_id = session_surfaces.memory_id
      AND t.kind = 'token_subseq'
      AND t.first_token IS NOT NULL
    LIMIT 1
)
WHERE first_token IS NULL;

CREATE INDEX IF NOT EXISTS idx_session_surfaces_failure_token
    ON session_surfaces(session_id, hook, first_token)
    WHERE hook = 'post_tool_use_failure' AND outcome IS NULL;
