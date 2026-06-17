"""Formation pipeline: body text → trigger candidates → persistence.

Four stages:
  - candidates: pure extraction from memory body (backticks, paths, URLs)
  - triggers:   write candidates to the triggers table
  - dedup:      detect overlap with existing memories, update in place
  - secrets:    safety gate — reject bodies that contain credentials
"""

from .candidates import (
    CandidateKind,
    FormationCandidate,
    consolidate_vocabulary,
    extract_candidates,
)
from .dedup import find_overlapping_memory, update_existing_memory
from .secrets import scan_for_secrets
from .similar import find_similar
from .triggers import extras_to_candidates, insert_candidate_triggers

__all__ = [
    "CandidateKind",
    "FormationCandidate",
    "consolidate_vocabulary",
    "extract_candidates",
    "extras_to_candidates",
    "find_overlapping_memory",
    "find_similar",
    "insert_candidate_triggers",
    "scan_for_secrets",
    "update_existing_memory",
]
