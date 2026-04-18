"""Retrieval pipeline: tool call → candidates → ranked/filtered surfaces.

Four stages:
  - extract:       parse a PreToolUse payload into head/path lookup hints
  - rank:          candidate retrieval, cluster stats, Laplace-smoothed filter
  - associations:  Hebbian co-activation (side-track boost on prior surfaces)
  - session_state: session_surfaces + session_turns read/write helpers
"""
