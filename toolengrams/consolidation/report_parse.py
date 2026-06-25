"""Parsing + validation for the consolidation agent's report envelope.

The consolidation agent ends its free-text report with one fenced ```json
object carrying `metrics` and (issue #64) `recommendations`. This module is the
single home for reading that envelope back out and judging whether it matches
the contract:

- `extract_json_block` — the trailing JSON object, or {} if none parses.
- `extract_metrics` / `extract_recommendations` — the typed, lenient views the
  recorder persists (numeric defaults, vocab coercion, malformed entries
  dropped).
- `validate_envelope` — a STRICT check used to decide whether to ask the agent
  to re-emit the block (same-session retry). It returns a list of human-readable
  problems (empty == valid) so the caller can both branch and quote them back to
  the agent.

The split is deliberate: persistence stays lenient (never lose a usable run over
a typo'd severity), while the retry gate is strict (a missing/garbled block is
worth one cheap correction round before we store a metric-less run).
"""

from __future__ import annotations

import json

# Fixed vocabularies for structured recommendations (issue #64). An unknown
# value is coerced to the safe default rather than dropping the recommendation —
# losing the advisory over a typo'd severity would be worse than mislabeling it.
REC_SEVERITIES = {"info", "warn", "critical"}
REC_STATUSES = {"open", "done"}


def extract_json_block(report: str) -> dict:
    """Parse the trailing ```json envelope from the consolidation report.

    The agent ends its report with one JSON object carrying `metrics` and
    `recommendations`. Returns the parsed object, or {} if there is no parseable
    trailing block (graceful degradation — the prose report is stored either
    way). Every other reader here reads from this single parse.
    """
    try:
        # Find the last JSON block in the report.
        last_brace = report.rfind("}")
        if last_brace < 0:
            return {}
        # Walk backwards to find the matching opening brace.
        depth = 0
        for i in range(last_brace, -1, -1):
            if report[i] == "}":
                depth += 1
            elif report[i] == "{":
                depth -= 1
            if depth == 0:
                parsed = json.loads(report[i:last_brace + 1])
                return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def extract_metrics(report: str) -> dict:
    """The `metrics` sub-dict from the report's JSON envelope.

    Falls back to the whole parsed object when there is no `metrics` key, so a
    bare `{...}` of metric fields (older agents) still parses.
    """
    block = extract_json_block(report)
    return block.get("metrics", block)


def extract_recommendations(report: str) -> list[dict]:
    """The validated `recommendations` array from the report's JSON envelope.

    Each entry is normalized to {title, severity, status, detail, issue_url}.
    Entries without a non-empty title are dropped (the title is the cross-run
    dedup key); severity/status outside the fixed vocab fall back to their
    defaults. Returns [] when the block is missing or `recommendations` is not a
    list — the prose report is stored regardless.
    """
    raw = extract_json_block(report).get("recommendations")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        severity = item.get("severity")
        status = item.get("status")
        detail = item.get("detail")
        issue_url = item.get("issue_url")
        out.append({
            "title": title,
            "severity": severity if severity in REC_SEVERITIES else "info",
            "status": status if status in REC_STATUSES else "open",
            "detail": str(detail).strip() if detail else None,
            "issue_url": str(issue_url).strip() if issue_url else None,
        })
    return out


def validate_envelope(block: dict) -> list[str]:
    """Problems with the report's JSON envelope; empty list == valid.

    Strict on the shape the system actually depends on — a parseable trailing
    object, a `metrics` dict, and (if present) a well-formed `recommendations`
    list — but NOT on every optional sub-field (those degrade leniently in the
    extractors). The returned strings are quoted back to the agent in the
    correction prompt, so they read as instructions, not error codes.
    """
    if not isinstance(block, dict) or not block:
        return ["the report did not end with a parseable JSON object"]
    problems: list[str] = []
    metrics = block.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        problems.append('the "metrics" object is missing or empty')
    recs = block.get("recommendations")
    if recs is not None:
        if not isinstance(recs, list):
            problems.append('"recommendations" must be a list')
        else:
            for i, item in enumerate(recs):
                if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                    problems.append(f'recommendation #{i + 1} is missing a "title"')
                    break
    return problems


def envelope_problems(report: str) -> list[str]:
    """Convenience: extract the trailing block and validate it in one step."""
    return validate_envelope(extract_json_block(report))
