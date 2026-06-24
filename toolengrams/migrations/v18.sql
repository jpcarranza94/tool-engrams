-- v18 — first-class consolidation recommendations (issue #64).
--
-- The consolidation agent already ends its report with a ```json metrics block;
-- it now emits a sibling `recommendations` array in the same envelope. Those
-- recommendations were previously buried in the prose report — readable per-run
-- but not trackable across runs. This table makes them first-class so the
-- dashboard can show TRENDS (what keeps recurring, is it handled) instead of
-- one-off text.
--
-- Keyed by run_date (which is UNIQUE in consolidation_runs). A --force re-run of
-- a day replaces that day's recommendations wholesale (insert_recommendations
-- deletes-then-inserts per run_date), so the table stays a faithful mirror of
-- the latest run for each date. New table only — no existing rows touched, so
-- memory reinforcement counters are untouched (counter-preserving by
-- construction). IF NOT EXISTS makes a re-run a no-op.
CREATE TABLE IF NOT EXISTS consolidation_recommendations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,                      -- 'YYYY-MM-DD'; consolidation_runs.run_date
    title       TEXT NOT NULL,                      -- short, stable label (dedup key across runs)
    severity    TEXT NOT NULL DEFAULT 'info',       -- info | warn | critical
    status      TEXT NOT NULL DEFAULT 'open',       -- open | done
    detail      TEXT,                               -- optional longer explanation
    issue_url   TEXT,                               -- optional link to a tracking issue
    created_ts  INTEGER NOT NULL,
    resolved_ts INTEGER                             -- set when status flips to 'done'
);

-- The cross-run dashboard query selects by the most recent run_dates, and the
-- per-run replace deletes by run_date — both index-served.
CREATE INDEX IF NOT EXISTS idx_consol_recs_run_date
    ON consolidation_recommendations(run_date);
