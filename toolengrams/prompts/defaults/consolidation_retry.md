Your previous response was received, but its final JSON block could not be used: {problems}.

Reply with ONLY the corrected JSON block — no prose, no commentary before or after. It must be a single fenced ```json block, and it MUST match this shape exactly:

```json
{{
  "metrics": {{
    "sessions_reviewed": <int>,
    "surfaces_evaluated": <int>,
    "surfaces_helpful": <int>,
    "surfaces_noise": <int>,
    "surfaces_neutral": <int>,
    "memories_created": <int>,
    "memories_pruned": <int>,
    "memories_verified": <int>,
    "total_active_after": <int>,
    "quality_score": <float 0.0-1.0>
  }},
  "recommendations": [
    {{"title": "<short stable label>", "severity": "info|warn|critical", "status": "open|done", "detail": "<context>"}}
  ]
}}
```

Reuse the same numbers and recommendations you already determined this run — only fix the format. If you made no recommendations, use an empty array (`"recommendations": []`). Do NOT re-run any tools or repeat your analysis.
