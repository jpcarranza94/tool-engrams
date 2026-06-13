# ADR-0010 — Codex engine containment uses runtime overrides plus the engram verb guard

- **Status:** Accepted
- **Context for:** PR4a, the Codex engine adapter (`codex exec`) for watcher
  ticks and consolidation

## Context

ToolEngrams now has an engine seam: the headless runner for watcher
formation/eval ticks and nightly consolidation can be swapped independently
from the hooked target. Claude Code uses project-local `.claude` settings for
its per-session command grants. Codex has a similar-looking project config
surface, but empirical testing against Codex 0.137.0 showed that fresh watcher
work dirs are untrusted projects: `.codex/config.toml` and `.codex/rules/`
files in those dirs are silently ignored.

The Codex engine still needs a hard boundary. A formation tick may call
`engram remember`; an eval tick may call `engram judge` and `engram
quarantine`; neither should be able to write arbitrary files, reach the
network, or mutate memory through disallowed `engram` verbs.

## Decision

`toolengrams/engine/codex.py` invokes Codex with runtime configuration
overrides rather than project files:

```text
codex exec --json --skip-git-repo-check --ephemeral \
  -s workspace-write \
  -c 'sandbox_workspace_write.writable_roots=["<db_dir>","<work_dir>"]' \
  -c 'sandbox_workspace_write.network_access=false' \
  -c 'sandbox_workspace_write.exclude_slash_tmp=true' \
  -c 'sandbox_workspace_write.exclude_tmpdir_env_var=true' \
  -c 'approval_policy="never"' \
  --cd <work_dir> [-m <model>] [--output-schema <schema_file>] \
  -o <last_message_file> -- <prompt>
```

The runtime `-c` layer is not project-trust-gated. The writable roots are the
directory containing `db.sqlite` and the fresh watcher/consolidation work dir.
Network access is disabled, `/tmp` and `$TMPDIR` are excluded, and approvals
are explicitly disabled. The `-o` last-message file and optional schema file
are parent-process files created under the engram home; they are not written by
the sandboxed child shell.

`prepare_sandbox()` intentionally writes no `.codex` files. The neutral
`SandboxSpec.command_prefixes` are enforced for watcher roles by the existing
engine-agnostic `$ENGRAM_ALLOWED_VERBS` guard in `toolengrams/__main__.py`.
That guard rejects every `engram` subcommand except the role's allowed verbs
inside watcher children. Consolidation remains the trusted broad-review agent,
as it already is for Claude Code.

Codex reports token usage in the JSONL `turn.completed.usage` event but not
USD cost. `EngineResult.cost_usd` is therefore `None`; the watcher run schema
already accepts null cost.

## Alternatives considered

- **Project-local `.codex/config.toml` / execpolicy rules:** rejected because
  Codex ignores those files in fresh untrusted work dirs. A security boundary
  that silently does not load is worse than no abstraction.
- **Rely only on `$ENGRAM_ALLOWED_VERBS`:** rejected because it constrains the
  ToolEngrams CLI, not shell writes, network access, or `/tmp`.
- **Override `CODEX_HOME` per run:** rejected because it would lose the user's
  Codex authentication (`~/.codex/auth.json`) and make headless runs look
  unauthenticated.

## Consequences

- Codex engine containment is visible in the argv and is covered by unit tests;
  there are no hidden project files whose loading depends on trust state.
- The model can still write `db.sqlite` directly because the DB directory must
  be writable for legitimate `engram remember` / `judge` calls. This is the
  same asset the child is allowed to mutate through the CLI. The accepted
  backstops are quarantine/consolidation audit, the kill switch, and keeping
  the command surface narrow for watcher roles.
- Codex engine auth remains user-managed through Codex (`codex login`,
  `CODEX_API_KEY`, or `OPENAI_API_KEY`). The adapter does not override
  `CODEX_HOME`.
