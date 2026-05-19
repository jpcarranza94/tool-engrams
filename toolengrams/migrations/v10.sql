-- Migration v9 → v10: consolidation_runs.memories_verified.
--
-- PR #18 added the git-aware staleness audit and `engram verify` CLI. The
-- consolidation prompt's metrics JSON schema instructs the agent to report
-- `memories_verified` per run, but the consolidation_runs table had no
-- column for it — so `cli/consolidate.py::_extract_metrics` silently
-- dropped the value, and quiet vs busy verification nights produced
-- indistinguishable run rows in the report.
--
-- Adding a nullable INTEGER column with default 0 keeps older rows
-- correct (they didn't run the staleness audit, so 0 is right).

ALTER TABLE consolidation_runs ADD COLUMN memories_verified INTEGER NOT NULL DEFAULT 0;
