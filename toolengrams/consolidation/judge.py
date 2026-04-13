"""LLM-judged consolidation: Haiku evaluates surfacing episodes and discovers missed memories.

Uses `claude -p` with Haiku to:
  1. Evaluate whether surfaced memories were helpful, neutral, or noise
  2. Discover corrections that should have become memories but weren't captured

Single batched call — all episodes in one prompt, one JSON response.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .episodes import CorrectionEpisode, SurfacingEpisode

CLAUDE_BIN = shutil.which("claude")

# Max episodes per LLM call (cost/token budget).
MAX_SURFACING_FOR_LLM = 20
MAX_CORRECTIONS_FOR_LLM = 10
MAX_DISCOVERIES = 5


@dataclass(slots=True)
class Verdict:
    memory_id: int
    judgment: str  # "helpful" | "neutral" | "noise"
    reason: str


@dataclass(slots=True)
class DiscoveredMemory:
    name: str
    body: str
    type: str  # "feedback" | "reference"
    scope: str  # "global" | "project"


@dataclass(slots=True)
class JudgmentResult:
    verdicts: list[Verdict] = field(default_factory=list)
    discoveries: list[DiscoveredMemory] = field(default_factory=list)
    raw_response: str = ""
    error: str | None = None


def judge_episodes(
    surfacing_episodes: list[SurfacingEpisode],
    correction_episodes: list[CorrectionEpisode],
    existing_memory_names: list[str],
) -> JudgmentResult:
    """Run Haiku to evaluate episodes. Returns structured verdicts + discoveries."""
    if not CLAUDE_BIN:
        return JudgmentResult(error="claude CLI not found on PATH")

    if not surfacing_episodes and not correction_episodes:
        return JudgmentResult()

    prompt = _build_prompt(
        surfacing_episodes[:MAX_SURFACING_FOR_LLM],
        correction_episodes[:MAX_CORRECTIONS_FOR_LLM],
        existing_memory_names,
    )

    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", "haiku", "--output-format", "json", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return JudgmentResult(error="claude -p timed out (60s)")
    except Exception as e:
        return JudgmentResult(error=f"claude -p failed: {e}")

    # Parse first JSON line from claude output.
    response_text = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                response_text = payload.get("result", "")
                break
            except json.JSONDecodeError:
                continue

    if not response_text:
        return JudgmentResult(
            raw_response=proc.stdout[:2000],
            error="No parseable response from Haiku",
        )

    return _parse_response(response_text, surfacing_episodes)


def _build_prompt(
    surfacing: list[SurfacingEpisode],
    corrections: list[CorrectionEpisode],
    existing_names: list[str],
) -> str:
    parts: list[str] = []

    parts.append(
        "You are evaluating a tool-bound memory system called ToolEngrams. "
        "Memories are bound to tool-call patterns and surface automatically "
        "when Claude calls matching tools.\n\n"
        "I'll show you two sets of episodes from today's sessions. "
        "Respond with ONLY a JSON object — no markdown, no explanation.\n"
    )

    # Section A: Surfacing evaluations.
    if surfacing:
        parts.append("## SURFACING EPISODES\n")
        parts.append("For each, judge whether the memory was helpful, neutral, or noise:\n")
        for i, ep in enumerate(surfacing):
            parts.append(
                f"Episode {i}: memory_id={ep.memory_id} name=\"{ep.memory_name}\"\n"
                f"  Tool: {ep.tool_name} | Input: {ep.tool_input_preview[:200]}\n"
                f"  Succeeded: {ep.tool_succeeded}\n"
                f"  Result: {ep.tool_result_preview[:200]}\n"
            )

    # Section B: Correction discovery.
    if corrections:
        parts.append("\n## CORRECTION EPISODES\n")
        parts.append(
            "These are user messages that look like corrections. "
            "Identify any that should become tool-bound memories. "
            f"Existing memories: {', '.join(existing_names[:20])}\n"
            f"Max {MAX_DISCOVERIES} new discoveries. Only include corrections "
            "about specific tool/command usage that contain backticked commands.\n"
        )
        for i, ce in enumerate(corrections):
            parts.append(
                f"Correction {i}: \"{ce.user_message[:300]}\"\n"
                f"  Preceding tool: {ce.preceding_tool} | Input: {ce.preceding_tool_input[:200]}\n"
            )

    # Response format.
    parts.append(
        "\n## RESPONSE FORMAT\n"
        "Respond with ONLY this JSON (no markdown fences):\n"
        "{\n"
        '  "verdicts": [\n'
        '    {"memory_id": 1, "judgment": "helpful|neutral|noise", "reason": "brief"}\n'
        "  ],\n"
        '  "discoveries": [\n'
        '    {"name": "short name", "body": "body with `backticked commands`", '
        '"type": "feedback|reference", "scope": "global|project"}\n'
        "  ]\n"
        "}\n"
    )

    return "\n".join(parts)


def _parse_response(
    text: str,
    surfacing: list[SurfacingEpisode],
) -> JudgmentResult:
    """Parse Haiku's JSON response into structured verdicts + discoveries."""
    # Try to extract JSON from the response (Haiku may wrap it in markdown).
    json_text = text.strip()
    if json_text.startswith("```"):
        lines = json_text.splitlines()
        json_lines = [l for l in lines if not l.startswith("```")]
        json_text = "\n".join(json_lines).strip()

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text.
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                return JudgmentResult(raw_response=text[:2000], error="Failed to parse Haiku JSON response")
        else:
            return JudgmentResult(raw_response=text[:2000], error="No JSON found in Haiku response")

    result = JudgmentResult(raw_response=text[:2000])

    # Parse verdicts.
    valid_memory_ids = {ep.memory_id for ep in surfacing}
    for v in data.get("verdicts", []):
        mid = v.get("memory_id")
        judgment = v.get("judgment", "neutral")
        if mid in valid_memory_ids and judgment in ("helpful", "neutral", "noise"):
            result.verdicts.append(Verdict(
                memory_id=mid,
                judgment=judgment,
                reason=v.get("reason", ""),
            ))

    # Parse discoveries (capped).
    for d in data.get("discoveries", [])[:MAX_DISCOVERIES]:
        name = d.get("name", "")
        body = d.get("body", "")
        if name and body and "`" in body:  # Must contain backticked commands.
            result.discoveries.append(DiscoveredMemory(
                name=name,
                body=body,
                type=d.get("type", "reference"),
                scope=d.get("scope", "global"),
            ))

    return result


def apply_verdicts(conn, verdicts: list[Verdict]) -> tuple[int, int]:
    """Apply LLM verdicts to memory scores. Returns (strengthened, weakened)."""
    strengthened = 0
    weakened = 0
    for v in verdicts:
        if v.judgment == "helpful":
            conn.execute(
                "UPDATE memories SET useful_count = useful_count + 1 WHERE id = ?",
                (v.memory_id,),
            )
            strengthened += 1
        elif v.judgment == "noise":
            conn.execute(
                "UPDATE memories SET surface_count = surface_count + 2 WHERE id = ?",
                (v.memory_id,),
            )
            weakened += 1
        # neutral → no change
    return strengthened, weakened


def apply_discoveries(conn, discoveries: list[DiscoveredMemory]) -> int:
    """Insert discovered memories via engram remember. Returns count inserted."""
    from ..commands.remember import main as remember_main

    inserted = 0
    for d in discoveries:
        argv = [
            d.body,
            "--type", d.type,
            "--scope", d.scope,
            "--name", d.name,
        ]
        rc = remember_main(argv)
        if rc == 0:
            inserted += 1
    return inserted
