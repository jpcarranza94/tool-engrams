-- Migration v2 → v3: Turn-based Hebbian co-activation.
-- Applied by db._migrate() when user_version < 3.
--
-- Wall-clock time is a poor measure of co-activation distance. A 5-minute gap
-- could be 20 tool calls of dense work or 1 call of deep thinking. This
-- migration replaces timestamp-based signal decay with turn-based: we count
-- tool calls per session and compute distance in turns, not seconds.

-- Turn counter per session — incremented on every PostToolUse.
CREATE TABLE IF NOT EXISTS session_turns (
    session_id TEXT PRIMARY KEY,
    turn_count INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL
);

-- Record turn position at the moment a memory surfaced (for Hebbian distance).
-- Existing rows will have NULL — treated as "unknown distance, skip in co-fire".
ALTER TABLE session_surfaces ADD COLUMN turn_at_surface INTEGER;

CREATE INDEX IF NOT EXISTS idx_session_surfaces_turn
    ON session_surfaces(session_id, turn_at_surface);
