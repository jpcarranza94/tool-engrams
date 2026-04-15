# ToolEngrams

Tool-bound automatic memory for Claude Code. Memories are tied to specific tool-call patterns, surface automatically when Claude is about to act, and strengthen with use — like biological engrams.

## What it does

When Claude is about to run a command (e.g., `git push --force`, `mycli -c`, `docker compose up`), ToolEngrams checks if there's a memory bound to that command prefix. If the memory is a correction (`feedback` type), the call is **blocked** until Claude reads the memory and retries with corrected arguments. If it's informational (`reference` type), the memory is injected as context alongside the call.

Memories form automatically. A background observer watches tool calls and flags patterns worth remembering. A nightly consolidation agent reviews the day's sessions, prunes noise, and discovers patterns that were missed.

## How memories surface

1. **Claude decides to call a tool** — e.g., `git push --force origin main`.

2. **Claude Code fires the PreToolUse hook** before executing the tool, piping the tool call details to `engram pretool` via stdin.

3. **The hook extracts the full command** for matching. `git push --force origin main` becomes the matchable string that stored triggers are checked against.

4. **SQLite is queried for matching memories.** Two kinds of triggers:
   - **Command prefix** (`tool_head`): `"git push --force"` matches `git push --force origin main` because it's a prefix. But `"git push"` is a different trigger on a different memory — only exact prefix matches fire.
   - **File path glob** (`path_glob`): `**/*.py` matches when Claude Reads/Edits/Writes any Python file.

5. **Matched memories are scored and filtered.** Usefulness (how often it helped), recency (when it last surfaced), and Hebbian association boosts (co-activation with other memories) determine which memories surface. Top 3 win. Session dedup ensures each memory surfaces only once per session.

6. **The hook responds — deny or allow.**
   - `feedback` memories with a command prefix trigger → **deny**. The tool call is blocked. Claude sees the memory as the reason, understands the correction, and retries with the right arguments.
   - `reference` memories or path glob triggers → **allow** with `additionalContext`. The tool runs normally but Claude sees the memory alongside the result.

7. **Claude acts on the memory.** If denied, it retries — e.g., `git push --force-with-lease origin main`. If allowed, the memory informs the current and future tool calls in the session.

8. **PostToolUse reinforces the outcome.** If the tool call succeeded after a memory surfaced, `useful_count` is incremented — strengthening the memory for future sessions.

9. **The async observer watches for new patterns.** PostToolUse also spawns a background Haiku agent that reviews recent context and decides if there's a new tool-usage pattern worth remembering.

## How memories form

Memories are created with explicit command prefix triggers — Claude (or the observer, or the consolidation agent) decides exactly which commands a memory should fire on:

```bash
# Correction: blocks git push --force, Claude must retry with --force-with-lease
engram remember "Use --force-with-lease instead of --force to avoid overwriting" \
  --trigger "git push --force" \
  --trigger "git push -f" \
  --type feedback

# Informational: context when using mycli (doesn't block)
engram remember "mycli connects to a read-only replica. SELECT only." \
  --trigger "mycli" \
  --type reference

# File-based: fires when editing Python test files
engram remember "Use pytest.raises as context manager, never decorator form" \
  --path "**/test_*.py" \
  --type feedback
```

Three formation layers work together:

| Layer | Model | When | Job |
|---|---|---|---|
| **Observer** | Haiku | After each nontrivial Bash call (async) | Fast candidate triage — flags patterns worth remembering |
| **Consolidator** | Opus | Daily at 6 PM | Thorough review — prunes noise, discovers missed patterns |
| **Manual** | N/A | User or Claude initiated | Escape hatch via `engram remember` |

## Neuroscience-inspired reinforcement

- **Hebbian learning** — memories that surface near each other in a session strengthen their association. Next time one fires, the other gets a score boost ("neurons that fire together wire together").
- **Usefulness scoring** — memories that surface before successful tool calls get reinforced. Memories that surface without helping decay over time.
- **Sleep consolidation** — a nightly Opus agent replays the day's sessions, evaluates memory quality, discovers missed patterns, and prunes noise. Like how sleep consolidates daily experiences into long-term memory.

## Install

```bash
git clone https://github.com/jpcarranza94/tool-engrams.git
cd tool-engrams
./install.sh
```

The install script:
1. Installs the `toolengrams` Python package (editable mode)
2. Adds hooks to `~/.claude/settings.json`
3. Symlinks skills (`/engram-remember`, `/engram-forget`, `/engram-recall`)
4. Seeds example memories
5. Optionally installs the 6 PM nightly consolidation schedule

### Requirements

- Python 3.10+
- Claude Code >= 2.1.59

### Manual install

If you prefer to set things up yourself:

```bash
# Install the package.
uv pip install --system -e .   # or: pip install -e .

# Verify.
engram status

# Add hooks to settings.json (see install.sh for the JSON structure).
# Symlink skills.
ln -sf "$(pwd)/skills/engram-remember" ~/.claude/skills/engram-remember
ln -sf "$(pwd)/skills/engram-forget" ~/.claude/skills/engram-forget
ln -sf "$(pwd)/skills/engram-recall" ~/.claude/skills/engram-recall

# Seed example memories.
engram seed

# Optional: install nightly consolidation.
engram consolidate --install-schedule
```

## CLI

```
engram recall [query]       Browse and search memories
engram recall --id N        Full detail on one memory
engram recall --stats       Summary counts
engram remember "<body>"    Manually create a memory (use --trigger, --type, --path)
engram forget "<name>"      Soft-demote a memory
engram pin "<name>"         Pin/unpin a memory
engram dashboard            Open HTML dashboard in browser
engram monitor              Resource usage and observer activity
engram status               Memory health JSON
engram consolidate          Run nightly consolidation now
engram consolidate --force  Re-run even if already ran today
engram seed                 Insert example memories
```

## Architecture

```
~/.claude/tool-engrams/
  db.sqlite          SQLite database (memories, triggers, associations, surfaces)
  observer.log       Observer activity log
  consolidate.log    Consolidation output

~/.claude/settings.json    Hook configuration (added by install.sh)
~/.claude/skills/          Skill symlinks (added by install.sh)
```

### Database schema

- **memories** — content, type (feedback/reference), scope (global/project), reinforcement counters
- **triggers** — command prefix (`Bash: git push --force`) or path glob (`**/*.py`) bindings
- **memory_associations** — Hebbian co-activation strength between memory pairs
- **session_surfaces** — which memories surfaced when (for dedup + reinforcement)
- **consolidation_runs** — nightly run log

### Scoring formula

```
usefulness = (useful_count + 1) / (surface_count + 2)         # Laplace-smoothed
recency    = exp(-days_since_last_surface / half_life)         # 30d feedback, 60d reference
final      = structural_match × (0.5 + usefulness) × (0.5 + 0.5 × recency)
           × (1 + association_boost)                           # Hebbian boost, max 30%
```

## Testing

```bash
# Unit tests (fast, 133 tests)
pytest

# E2E tests (spawns real claude -p sessions, opt-in)
pytest tests/e2e/ -m e2e
```

## Uninstall

```bash
# Remove hooks from settings.json (manually edit or re-run without the hook entries).
# Remove skills.
rm ~/.claude/skills/engram-{remember,forget,recall}
# Remove the consolidation schedule.
engram consolidate --uninstall-schedule
# Remove the database.
rm -rf ~/.claude/tool-engrams/
# Uninstall the package.
pip uninstall toolengrams
```
