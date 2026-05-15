-- Migration v7 → v8: memories.last_verified_ts.
--
-- Adds an explicit "this memory was checked against current reality on
-- <ts>" timestamp. The nightly consolidation agent inspects each memory
-- and either:
--   - archives it (memory now contradicts git history / current code), or
--   - sets last_verified_ts = NOW (memory still holds).
--
-- Default NULL means "never verified" — agent treats as fresh, low-priority
-- to re-check. After first verification, agent re-checks if older than the
-- staleness horizon (currently 14d, set in the consolidation prompt).

ALTER TABLE memories ADD COLUMN last_verified_ts INTEGER DEFAULT NULL;
