# ADR-0001 — Watcher sessions evaluate via the engram CLI, not a constrained JSON schema

- **Status:** Accepted
- **Context doc:** `docs/design.md` §1, §2, §5

## Context

A watcher session (formation, evaluation, consolidation) is an LLM that changes the
memory store. Each needs an output contract — a way for the model's conclusions to
become DB writes. There are two mechanisms:

1. **Labeler-via-JSON** — the model returns a constrained JSON object (e.g.
   `{verdicts: [...]}`), and deterministic parent code parses it and writes the DB.
2. **Labeler-via-CLI** — the model calls `engram judge <id> <outcome>` (or `engram
   remember …`) itself; the harness does not parse anything.

The JSON path is fragile in practice: constrained decoding plus fenced-JSON output means
the parser needs multiple fallback extraction strategies and a parse-error retry branch,
and a single failed parse loses every result in the batch.

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
  skips done work. (A JSON batch loses all verdicts on a failed parse.)
- One mental model across all three watchers.

**Negative / costs**
- Tool-calling is multi-round-trip → more tokens/latency per tick than one JSON blob.
  Acceptable for background work; bounded by per-session surface counts.
- A tool-calling session can't be `--bare`; it needs the permissioned temp-cwd pattern
  (`write_agent_settings` — since the engine seam, `engine.prepare_sandbox` — + an internal-cwd recursion guard).
- Observability lives in the CLI commands (the parent doesn't see a parsed result).
- Formation gains a duplicate-memory risk under held-window retry → mitigated by
  `engram remember` deduping on name.

## Alternatives rejected

- **Labeler-via-JSON** (option 1): fragile parsing, all-or-nothing on failure, and a
  second output contract to maintain.
- **Eval as a full agent with the whole toolbox**: gives a per-turn judge a delete
  button on the live store; one twitchy verdict could archive a good situational memory.
  Restricting the command surface to `engram judge` keeps the labeler's bounded blast
  radius while still using CLI calls.
