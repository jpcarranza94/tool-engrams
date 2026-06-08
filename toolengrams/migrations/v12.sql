-- Migration v11 → v12: the scoring cutover.
--
-- Three changes:
--
--   (1) memories.noise_count — a new counter the evaluation watcher bumps on a
--       'noise' verdict (the trigger over-matched; the content may be fine).
--       Paired with useful_count, it feeds the noise-aware quality ratio
--           q = (useful_count + 1) / (useful_count + noise_count + 2)
--       that drives ranking and the surfacing gate.
--
--   (2) useful_count reset to 0 — the old PostToolUse hook credited a surfaced
--       memory whenever the tool call merely *succeeded*, so the column is
--       saturated (101/111 active memories at useful ≈ surface) and carries no
--       real signal. Wipe it; the eval watcher rebuilds honest counts. Every
--       memory lands at q = 0.5 (neutral), protected by the warm-up gate while
--       it re-earns 'helpful'. surface_count is KEPT as telemetry (lets
--       consolidation see "surfaced 50×, never judged").
--
--   (3) watcher_state re-keyed (work_session_id, role) — formation and eval
--       are independent watcher sessions with their own cursors / resume ids /
--       retry streaks. Rather than double every column, the PK gains a `role`
--       (formation | eval): two symmetric rows per session, same schema.
--       Existing rows become role='formation'; eval rows are created on demand.
--
-- Rollback: (1) is additive; (2) is a one-way data wipe (acceptable — alpha,
-- the data is noise); (3) needs the table rebuilt back to a single-column PK.

-- (1) + (2): new counter, polluted counter wiped.
ALTER TABLE memories ADD COLUMN noise_count INTEGER NOT NULL DEFAULT 0;
UPDATE memories SET useful_count = 0;

-- (3): re-key watcher_state. SQLite can't ALTER a PRIMARY KEY, so rebuild.
-- watcher_state carries no indexes/triggers/FKs, so the rebuild is just
-- create-copy-drop-rename; existing rows get role='formation'.
ALTER TABLE watcher_state RENAME TO watcher_state_tmp;

CREATE TABLE watcher_state (
    work_session_id    TEXT NOT NULL,
    role               TEXT NOT NULL DEFAULT 'formation'
                         CHECK (role IN ('formation','eval')),
    watcher_session_id TEXT,
    watcher_pid        INTEGER,
    transcript_path    TEXT,
    last_line_read     INTEGER NOT NULL DEFAULT 0,
    last_checked_ts    INTEGER NOT NULL,
    cwd                TEXT,
    created_ts         INTEGER NOT NULL,
    armed              INTEGER NOT NULL DEFAULT 0,
    last_tick_ts       INTEGER NOT NULL DEFAULT 0,
    fail_streak        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (work_session_id, role)
);

INSERT INTO watcher_state
    (work_session_id, role, watcher_session_id, watcher_pid, transcript_path,
     last_line_read, last_checked_ts, cwd, created_ts, armed, last_tick_ts, fail_streak)
SELECT
    work_session_id, 'formation', watcher_session_id, watcher_pid, transcript_path,
    last_line_read, last_checked_ts, cwd, created_ts, armed, last_tick_ts, fail_streak
FROM watcher_state_tmp;

DROP TABLE watcher_state_tmp;
