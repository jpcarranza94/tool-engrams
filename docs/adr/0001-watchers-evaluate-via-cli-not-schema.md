# ADR-0001 — Watcher sessions evaluate via the engram CLI, not a constrained JSON schema

- **Status:** Accepted (2026-06-05)
- **Context doc:** `docs/design-v10.md` §2, §3, §6

## Context

A watcher session (formation, evaluation, consolidation) is an LLM that changes the
memory store. v9's formation watcher returned a **constrained JSON schema**
(`WATCHER_SCHEMA`) that the harness parsed and acted on. The new evaluation watcher
needed the same kind of output contract. We considered two mechanisms:

1. **Labeler-via-JSON** — the model returns `{verdicts: [...]}`, deterministic parent
   code parses it and writes the DB.
2. **Labeler-via-CLI** — the model calls `engram judge <id> <outcome>` itself; the
   harness does not parse anything.

The JSON path is already fragile in v9: the parser needs three fallback extraction
strategies (`_candidate_json_strings`) and a `parse_error` retry branch *because*
constrained decoding plus fenced-JSON parsing breaks in practice.

## Decision

**All watcher sessions evaluate by calling `engram` CLI commands. No constrained JSON
schema.** The CLI is the interface; the harness stops marshaling model output.

Safety is held by restricting each watcher's **command surface**, not by a schema:

- formation → `engram remember` only
- evaluation → `engram judge` only
- consolidation → full `engram *` (the destructive/reversible levers)

The CLI command is the validation boundary (rejects unknown / out-of-session ids and
bad arguments, is idempotent, logs its own action).

## Consequences

**Positive**
- Native tool-calling instead of fragile JSON parsing.
- **Deferral falls out of not-calling** — a judge the model omits stays pending and is
  re-judged next pass.
- **Partial failure is safe** — each CLI call already committed; an idempotent retry
  skips done work. (JSON lost all verdicts on a failed parse.)
- One mental model across all three watchers.
- Deletes harness code: `WATCHER_SCHEMA`, `_parse_response`, `_candidate_json_strings`,
  `_save_memory`, the `parse_error` retry.

**Negative / costs**
- Tool-calling is multi-round-trip → more tokens/latency per tick than one JSON blob.
  Acceptable for background work; bounded by per-session surface counts.
- A tool-calling session can't be `--bare`; it needs the permissioned temp-cwd pattern
  (`write_agent_settings` + `is_internal_cwd`). Recursion-avoidance moves accordingly.
- Observability moves into the CLI commands (the parent no longer sees a parsed result).
- Formation gains a duplicate-memory risk under held-window retry → mitigated by
  `engram remember` deduping on name.

## Alternatives rejected

- **Labeler-via-JSON** (option 1): fragile parsing, all-or-nothing on failure, and a
  second output contract to maintain.
- **Eval as a full agent with the whole toolbox**: gives a per-turn judge a delete
  button on the live store; one twitchy verdict could archive a good situational memory.
  Restricting the command surface to `engram judge` keeps the labeler's bounded blast
  radius while still using CLI calls.
