# ToolEngrams — design v8 (frozen 2026-04-11)

## Problem

Claude Code has a file-based auto-memory system at `~/.claude/projects/<project>/memory/`. The harness loads `MEMORY.md` at session start (200 lines / 25 KB hard cap) and expects Claude to manually `Read` topic files when relevant. In practice Claude re-reads its own `CLAUDE.md` and `MEMORY.md` repeatedly — session analysis across 30 sampled sessions found `MEMORY.md` was read 14 times and `CLAUDE.md` was read or `cat`ed 29 times. Every manual re-read is a failed recall.

We want memories surfaced **automatically**, at the moment they become relevant, without Claude having to remember to look.

## Core insight (the novel part)

Memory is not content indexed by semantic similarity — it's **content bound to tool-call patterns**. Every memory has an explicit answer to "when should this fire?" expressed as a predicate over `(tool_name, tool_input)`. When Claude is about to call a tool, the hook retrieves memories bound to that exact tool-call pattern, not memories whose content happens to be similar to the prompt.

This is a generalization of claude-mem's file-observation pattern (which binds observations to file paths, for `Read` only) to all tools with richer pattern types.

## Why the pattern primitive is "token-sequence prefix", not regex

Session-analyst's 30-session corpus analysis showed:

- **Bash dominates** at 40% of all tool calls (226 of 562 sampled).
- **97% lexical uniqueness** on full Bash command strings (221 unique out of 227).
- **But very tight head-token clustering:** 259 distinct `(first_token, second_token)` buckets cover the entire corpus, top 10 cover 58%, top 30 cover 78.5%, top 50 cover 85.3%.
- Most matches go to short head sequences: `ssh`, `git`, `gh`, `jira`, `curl`, `mycli`, `aws`, `grep`, `ls`, `python3`.

Conclusion: a **tiered structural match on token-sequence prefixes** is sufficient and cheap. Regex is overkill, embeddings are deferred to v2 as a cold-path fallback if structural match leaves too much on the table.

A head like `[ssh, "deploy@"]` prefix-matches both `ssh deploy@10.0.1.50` and `ssh deploy@10.0.1.51` — same workflow, one memory.

## Components

### 1. Canonical store: SQLite at `~/.claude/tool-engrams/db.sqlite`

Separate directory, **not** inside `~/.claude/projects/`. Zero interference with Claude Code's harness-managed memory dir. Schema lives in `toolengrams/schema.sql`.

Tables:
- `memories` — content, type, scope (global/project), lifecycle metrics
- `triggers` — typed-row table with `kind` discriminator: `tool_head | path_glob | error_contains | keyword`
- `session_surfaces` — ring buffer per session for `/forget` disambiguation + session dedup
- `memories_fts` — FTS5 virtual table for keyword fallback and deep browse

WAL mode, `PRAGMA synchronous=NORMAL`, `PRAGMA foreign_keys=ON`.

### 2. No daemon (v1)

Every hook is a self-contained `engram` subprocess (CLI for the `toolengrams` package). SQLite from cold Python is fast:
- `import sqlite3` is stdlib — no torch-scale import cost
- Opening the DB: microseconds
- Single tool-head lookup: single-digit ms
- Reinforcement update + session ring buffer write: atomic

The daemon was justified in earlier drafts by `sentence-transformers` cold start (1.5–3s per hook). v1 has no embeddings, so no daemon. v2 may reopen this if local semantic rerank is added.

### 3. Retrieval layers

| Layer | Hook | Role |
|---|---|---|
| **Identity** | `SessionStart` | Eager-inject `type: user` + pinned + recent project memories. Reinforcement-exempt. |
| **Topical** | `UserPromptSubmit` | Rank by keyword + path + trigger match against prompt text. |
| **Mid-turn** ★ | `PreToolUse` | Tool-call-bound structural match. Top-3 injected via `hookSpecificOutput.additionalContext`. |
| **Recovery** | `PostToolUseFailure` | Substring match on error text + tool head. |

★ = the novel surface. Claude-mem and everyone else mostly trigger at the turn boundary; this is where ToolEngrams' primary value lives.

### 4. Scoring: reinforcement formula

```
usefulness = (useful_count + 1) / (surface_count + 2)       # Laplace-smoothed
recency    = exp(-Δt / half_life)                            # 14d project, 30d feedback, 60d user/ref
final      = structural_match × (0.5 + usefulness) × (0.5 + 0.5 × recency) × (1.5 if pinned)
```

### 5. Relevance filter: per-cluster Laplace-smoothed threshold

The threshold for "inject or not" is not a global constant. It's normalized per `(tool, head_joined)` cluster with a prior that handles cold start:

```
smoothed_cluster_mean = (Σ final_score + prior_mean × prior_weight) / (n_memories + prior_weight)
threshold = max(smoothed_cluster_mean × cluster_factor, absolute_floor)
inject if memory.final_score > threshold
```

Defaults: `prior_mean=0.3`, `prior_weight=3`, `cluster_factor=0.9`, `absolute_floor=0.15`.

This is what makes new memories injectable (prior handles empty clusters) while keeping weak memories out of mature clusters.

**Tier 2 (deferred to v1.5):** Haiku as a relevance filter for genuinely ambiguous cases (2+ candidates, close scores). v1 ships with Tier 1 only and logs ambiguity for measurement.

### 6. Reinforcement signals

| Signal | Delta |
|---|---|
| Surfaced + next tool-call args overlap memory body | **+1.0** |
| Surfaced + next tool call didn't error | +0.5 |
| Surfaced + user didn't correct in 2 turns | +0.5 |
| User says "yes"/"good"/"that helped" | +1 |
| User correction / "forget X" / "don't X" | **−2** |

**Polling guard:** identical repeated Bash calls accrue at most one `surface_count` increment per session (avoids reinforcement farming on health-check loops).

### 7. Formation workflow

`/remember <text>` skill (Claude or user invoked) runs:

1. **Deterministic extraction** — parse body for backticked shell snippets (`(first_token, second_token)`), tilde/absolute paths, URL hosts, known CLI names (`git gh jira bq psql mycli ssh curl docker aws python3 make`).
2. **Vocabulary consolidation** — query existing triggers; reuse those appearing in ≥2 memories (convergence by gravity).
3. **Proposal** — Claude-authored auto-accepts; user-initiated sees a one-line confirm.
4. **Insert** into SQLite with candidate triggers.

### 8. Forget mechanism

- `/forget <name>` → soft demote: `useful_count=0, surface_count+=5, last_surfaced_ts=0`
- `/forget --delete <name>` → set `archived_ts`, excluded from retrieval
- `/forget --topic <keyword>` → soft-demote all matching
- NL regex `\b(forget|ignore|don't remember)\b` on user prompt + unique recent surface → soft demote only
- `/remember --restore <name>` → undo

NL detection never hard-deletes. Soft demote by default.

### 9. Memory types × scope

- **type**: `user | feedback | project | reference` — semantics from the existing auto-memory docs
- **scope**: `global | project` — project is default. Global memories live in the same DB with `scope='global'`, joined at query time with the current project slug.

### 10. Trigger schema

```yaml
triggers:
  tools:                                          # prefix-matched head sequences
    - {tool: Bash, head: [mycli]}
    - {tool: Bash, head: [ssh, "deploy@"]}      # prefix on second token
    - {tool: Bash, head: [git, push]}
    - {tool: WebFetch, head: ["api.github.com"]}
  paths:                                          # Read/Edit/Write.file_path AND Bash-text paths
    - "**/settings.json"
  errors:                                         # substring on stderr
    - {tool: Bash, head: [ssh], error_contains: "Connection refused"}
  keywords: [read-only, replica]                  # last-resort fallback
```

Longest-match is the **tiebreaker** when multiple bindings match the same call — not a global priority. `[git]` and `[ssh, deploy@]` coexist because they bind different calls.

### 11. Tool whitelist for PreToolUse bindings

Only these tools carry user-facing bindings: **Bash, Read, Edit, Write, Grep, Glob, WebFetch, NotebookEdit**.

Excluded: `SendMessage`, `TaskUpdate`, `Agent`, `ToolSearch`, `TeamCreate`, `CronCreate`, etc. — no stable lexical handle mapping to user workflow.

## What's truly novel vs prior art

1. **Tool-bound memory corpus via tiered structural triggers** — claude-mem does this only for Read+file_path. We generalize to 8 tools with prefix-matched head sequences and unified path extraction.
2. **Brain-like reinforcement with cheap implicit usefulness signals** — no prior-art project does this.
3. **Formation-time vocabulary consolidation** — self-organizing binding clusters.
4. **In-session soft-forget via NL detection** — targeted decay without destruction.
5. **`PostToolUseFailure` recovery recall** — verified unique across the surveyed corpus.
6. **No-daemon architecture** — every prior-art project we surveyed had a server. This one doesn't need one.
7. **Per-cluster Laplace-smoothed relevance threshold** — handles cold start cleanly without hand-tuned global constants.

## Open items explicitly deferred to v1.5 / v2

- Embeddings-based semantic fallback (sentence-transformers or similar) for memories where structural match leaves ambiguity
- Haiku-as-judge relevance filter for ambiguous multi-candidate cases
- Pattern drift auto-migration (path renames via git log)
- LLM-judged usefulness at session end
- One-shot import of `CLAUDE.md` trigger-phrase rules into the memory store
- Plugin packaging for one-command install
