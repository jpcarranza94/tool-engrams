-- v17 — path-glob access mode (issue #63).
--
-- Path-glob triggers used to fire identically on read-only tools (Read/Grep/
-- Glob) and mutating ones (Edit/Write/MultiEdit/NotebookEdit). Edit-intended
-- memories surfaced on mere reads — the dominant noise mode (2026-06-23
-- consolidation, quality_score 0.37). triggers.access_mode now carries the
-- read-vs-write intent so match_path_triggers can filter on it.
--
-- Backfill: existing path_glob triggers default to 'write' — most file-path
-- lessons are about mutation, and this delivers the noise fix immediately
-- (rather than waiting for re-formation). A path memory that legitimately
-- should fire on reads can be re-tuned with `engram trigger --access-mode`.
-- Counter-preserving: touches only the new column on path_glob rows; memory
-- counters (useful_count/noise_count/surface_count) are untouched.
-- token_subseq rows keep access_mode = NULL (the column is meaningless there).

-- Wrapped in a transaction so the ADD COLUMN and the backfill apply atomically.
-- Without it (executescript runs in autocommit), a failure after the ALTER but
-- before/within the UPDATE would leave the column added yet user_version still
-- at 16, and the next connect would re-run the ALTER and die on
-- "duplicate column name". SQLite can roll back DDL, so the transaction makes
-- the whole migration all-or-nothing and safely re-runnable.
BEGIN;
ALTER TABLE triggers ADD COLUMN access_mode TEXT;
UPDATE triggers SET access_mode = 'write' WHERE kind = 'path_glob';
COMMIT;
