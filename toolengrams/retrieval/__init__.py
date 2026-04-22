"""Retrieval pipeline: tool call → candidates → ranked/filtered surfaces.

Three stages:
  - extract:       parse a tool-call payload into token/path lookup hints
  - rank:          candidate retrieval, cluster stats, Laplace-smoothed filter
  - session_state: session_surfaces + session_turns read/write helpers
"""
