-- Migration v3 → v4: Consolidation quality tracking.
-- Adds quality_score to consolidation_runs for day-over-day comparison.

ALTER TABLE consolidation_runs ADD COLUMN quality_score REAL;
ALTER TABLE consolidation_runs ADD COLUMN surfaces_helpful INTEGER NOT NULL DEFAULT 0;
ALTER TABLE consolidation_runs ADD COLUMN surfaces_noise INTEGER NOT NULL DEFAULT 0;
