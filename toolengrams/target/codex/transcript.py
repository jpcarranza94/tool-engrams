"""Codex rollout JSONL -> canonical watcher delta."""

from __future__ import annotations

import json

from ..transcript_utils import (
    MAX_BASH_CMD_CHARS,
    MAX_RESULT_CHARS,
    _cap_delta,
    _clip_ends,
    _clip_head,
)
from .patch_parse import paths_from_patch


def format_delta(lines: list[str]) -> str:
    parts: list[str] = []
    for raw_line in lines:
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            parts.append(f"RESULT: {_clip_ends(raw_line.strip(), MAX_RESULT_CHARS)}")
            continue

        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            parts.append(f"RESULT: {_clip_ends(raw_line.strip(), MAX_RESULT_CHARS)}")
            continue

        try:
            _append_payload(parts, payload)
        except Exception:
            parts.append(f"RESULT: {_clip_ends(raw_line.strip(), MAX_RESULT_CHARS)}")

    return _cap_delta("\n".join(parts))


def _append_payload(parts: list[str], payload: dict) -> None:
    payload_type = payload.get("type")
    if payload_type == "message":
        _append_message(parts, payload)
    elif payload_type == "function_call":
        _append_function_call(parts, payload)
    elif payload_type == "function_call_output":
        output = str(payload.get("output") or "")
        if output:
            parts.append(f"RESULT: {_clip_ends(output, MAX_RESULT_CHARS)}")
    elif payload_type == "custom_tool_call":
        _append_custom_tool_call(parts, payload)
    elif payload_type == "custom_tool_call_output":
        output = str(payload.get("output") or "")
        if output:
            parts.append(f"RESULT: {_clip_ends(output, MAX_RESULT_CHARS)}")


def _append_message(parts: list[str], payload: dict) -> None:
    role = payload.get("role")
    if role == "developer":
        return
    text = _content_text(payload.get("content"))
    if not text or _is_boilerplate_user(text):
        return
    if role == "user":
        parts.append(f'USER: "{_clip_head(text, 500)}"')
    elif role == "assistant":
        parts.append(f'AGENT: "{_clip_head(text, 300)}"')


def _content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts).strip()


def _is_boilerplate_user(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<environment_context>") or stripped.startswith("<permissions")


def _append_function_call(parts: list[str], payload: dict) -> None:
    name = str(payload.get("name") or "unknown")
    arguments = payload.get("arguments")
    parsed = _parse_arguments(arguments)
    if name == "exec_command":
        cmd = str(parsed.get("cmd") or parsed.get("command") or arguments or "")
        parts.append(f"TOOL (Bash): {_clip_head(cmd, MAX_BASH_CMD_CHARS)}")
    else:
        parts.append(f"TOOL ({name}): {_clip_head(str(arguments or ''), MAX_BASH_CMD_CHARS)}")


def _parse_arguments(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _append_custom_tool_call(parts: list[str], payload: dict) -> None:
    name = str(payload.get("name") or "unknown")
    patch = str(payload.get("input") or "")
    if name == "apply_patch":
        paths = paths_from_patch(patch)
        summary = ", ".join(paths) if paths else _clip_head(patch, MAX_BASH_CMD_CHARS)
        parts.append(f"TOOL (apply_patch): {summary}")
    else:
        parts.append(f"TOOL ({name}): {_clip_head(patch, MAX_BASH_CMD_CHARS)}")
