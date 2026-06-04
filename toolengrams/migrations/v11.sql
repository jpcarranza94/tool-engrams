-- Migration v10 → v11: event-driven watcher state.
--
-- The watcher moves from a 5-minute cron poll to hook-driven ticks (fired on
-- Stop / session-end / a detected failure→success / a user correction). Three
-- new watcher_state columns carry the state the poll used to keep in process
-- memory:
--
--   armed       — set to 1 by PostToolUseFailure; upgrades an otherwise
--                 skippable turn-boundary tick into a definite model call so
--                 an error→fix episode is never gated out. Cleared each tick.
--   last_tick_ts— wall-clock of the last completed tick; the hook side uses it
--                 to COALESCE a burst of triggers into one model call.
--   fail_streak — consecutive failed attempts on the current transcript window,
--                 persisted ACROSS events (the poll kept it in a local var).
--                 Bounds cross-event retries so a poison window can't wedge.

ALTER TABLE watcher_state ADD COLUMN armed        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watcher_state ADD COLUMN last_tick_ts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE watcher_state ADD COLUMN fail_streak  INTEGER NOT NULL DEFAULT 0;
