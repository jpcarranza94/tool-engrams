-- v16: dual-harness target/engine attribution.
-- watcher_state.target names the harness whose transcript format the
-- detached tick must parse (ADR: dual-harness work); watcher_runs.engine
-- records which engine adapter ran the call (cost/engine attribution in
-- `engram monitor`).
ALTER TABLE watcher_state ADD COLUMN target TEXT NOT NULL DEFAULT 'claude-code';
ALTER TABLE watcher_runs ADD COLUMN engine TEXT;
