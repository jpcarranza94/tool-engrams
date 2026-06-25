"""The engine adapter contract.

An *engine* is the headless runner ToolEngrams shells out to for background
LLM work: watcher formation/eval ticks and nightly consolidation. Adapters
are plain modules (not class instances) registered in `selection.ENGINES`;
this Protocol exists for typing and the conformance test — module objects
satisfy it structurally.

Containment is part of the contract, but engines do not expose identical
native controls. `prepare_sandbox` translates the neutral `SandboxSpec` into
the enforceable controls the adapter has (claude-code: a
`.claude/settings.local.json` allowlist; codex: no trust-gated project files,
with sandbox flags applied at invoke time). The engine-agnostic backstop —
the `$ENGRAM_ALLOWED_VERBS` dispatch guard in `__main__.py` — is set by the
caller, not the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .result import EngineResult


@dataclass(slots=True)
class SandboxSpec:
    """Engine-neutral description of what an agent session may do.

    `command_prefixes` are shell-command prefixes (e.g. "engram remember");
    each adapter maps them to its native grant form. `readable_paths` are
    single files the session must be able to read (the delta drop).
    `readonly_explore` adds the consolidation agent's broad read-only
    surface (file reading/search tools).
    """

    command_prefixes: tuple[str, ...]
    readable_paths: tuple[str, ...] = ()
    readonly_explore: bool = False


@dataclass(slots=True)
class EngineRequest:
    prompt: str
    timeout: int
    # Drives the adapter's model resolution ("formation" | "eval" |
    # "consolidation"); an explicit `model` overrides it.
    role: str | None = None
    model: str | None = None
    cwd: str | None = None
    env: dict | None = None
    # JSON Schema text for constrained output, when the engine supports it.
    schema: str | None = None
    # Continue an existing conversation instead of starting fresh: pass a
    # session_id returned by a prior EngineResult. Honored by engines that
    # persist sessions (claude-code → `--resume`); ignored where there is no
    # session to resume (codex `--ephemeral`).
    resume_session_id: str | None = None


@runtime_checkable
class EngineAdapter(Protocol):
    NAME: str
    min_version: str | None

    def is_available(self) -> bool: ...

    def installed_version(self) -> str | None: ...

    def resolve_model(self, role: str | None = None) -> str | None: ...

    def prepare_sandbox(self, work_dir: Path, spec: SandboxSpec) -> None: ...

    def invoke(self, req: EngineRequest) -> EngineResult: ...
