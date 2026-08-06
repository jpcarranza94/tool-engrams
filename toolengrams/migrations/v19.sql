-- v19 — root relative path_glob triggers at '**/'.
--
-- Stored globs are fnmatched against the paths retrieval/extract.py emits, and
-- those are always absolute ('/...' or '~/...'). fnmatch has no implicit anchor,
-- so a pattern that doesn't start with '/', '~' or '*' can never match anything.
-- 47 rows (38 on active memories, 29 distinct memories) in the live DB were dead
-- on arrival; a replay of 39k historical tool calls puts them at 1 match today
-- vs 4,954 once rooted. formation/candidates.py now normalizes at construction;
-- this backfills the rows written before that.
--
-- instr(path_pattern, '/') > 0 is the SQL proxy for the Python specificity gate:
-- rooting a bare basename yields '**/<basename>', which path_glob_is_specific_enough
-- refuses ('**/README.md' would fire on 691 corpus calls). Those rows are left
-- alone — the migration only ever declines to widen, never widens something
-- formation would have refused. Three rows stay unmatchable by design:
-- 'README.md', 'serverless.yml', 'vulture_whitelist.py'.
--
-- The DELETE handles the one memory that already carries BOTH spellings of the
-- same glob; without it the UPDATE would leave a duplicate row (triggers has no
-- unique constraint, so it would be silent).
--
-- Counter-preserving: touches only triggers.path_pattern.
-- Wrapped in a transaction for the same reason as v17 (executescript runs in
-- autocommit; a half-applied backfill would re-run and double-prefix, and
-- '**/**/x' still fnmatches so the corruption would be invisible). The selector
-- is also self-excluding, so a re-run is a genuine no-op either way.
BEGIN;
DELETE FROM triggers
 WHERE kind = 'path_glob'
   AND path_pattern NOT GLOB '[/~*]*'
   AND instr(path_pattern, '/') > 0
   AND EXISTS (SELECT 1 FROM triggers t2
                WHERE t2.memory_id = triggers.memory_id
                  AND t2.kind = 'path_glob'
                  AND t2.path_pattern = '**/' || triggers.path_pattern);

UPDATE triggers SET path_pattern = '**/' || path_pattern
 WHERE kind = 'path_glob'
   AND path_pattern NOT GLOB '[/~*]*'
   AND instr(path_pattern, '/') > 0;
COMMIT;
