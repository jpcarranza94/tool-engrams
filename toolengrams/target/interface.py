"""The target adapter contract.

A *target* is the coding-agent harness whose tool calls we hook — memories
surface into it, its transcripts feed the watcher, its sessions feed the
nightly consolidation. Adapters are plain modules (not class instances)
registered in `target/__init__.TARGETS`; this Protocol exists for typing and
the conformance test — module objects satisfy it structurally.

Several targets can be wired against one DB at once (the engine, by
contrast, is a single active choice). Every hook invocation carries its
target via the `--target` flag baked into the wired hook command at install
time — payloads are NOT sniffed (codex deliberately mirrors claude's hook
payload shape, so sniffing is ambiguous).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..retrieval.extract import ExtractedTriggerHint


@dataclass(slots=True)
class SessionFile:
    """One harness session transcript on disk, as the consolidation
    collector reports it."""

    path: Path
    session_id: str
    project_slug: str
    modified_ts: float
    size_bytes: int


@runtime_checkable
class TargetAdapter(Protocol):
    NAME: str
    # Tool names whose pre/post-failure events carry memory bindings.
    tool_whitelist: frozenset[str]
    # Minimum harness version the install supports (doctor enforces).
    min_version: str
    # CLI binary name doctor checks when this target is wired.
    cli_binary: str
    # Whether the target has a dedicated PostToolUseFailure hook event.
    has_failure_event: bool

    def extract_hints(self, tool_name: str, tool_input: dict) -> ExtractedTriggerHint: ...

    def detect_failure(self, payload: dict) -> bool: ...

    def transcript_path(self, payload: dict) -> str: ...

    def format_delta(self, lines: list[str]) -> str: ...

    # Return sessions the target considers part of target_date. Some harnesses
    # store sessions by modified time; Codex stores rollout files under
    # YYYY/MM/DD directories, so its collector follows that storage day.
    def collect_sessions(self, target_date: date,
                         projects_dir: Path | None = None) -> list[SessionFile]: ...

    def hook_markers(self) -> dict[str, str]: ...

    def hook_status(self) -> dict[str, object]: ...

    def installed_version(self) -> str | None: ...

    def is_wired(self) -> bool: ...
