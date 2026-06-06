"""Reinforcement: how a memory's score evolves over its lifetime.

  - scoring: q (noise-aware usefulness) + pin boost, applied to candidates;
    plus the surfacing gate that suppresses hints proven more noise than signal.

The side effects that move the inputs to that formula (surface_count,
useful_count, soft-demote, archive, restore) live in `memory_store` — they are
writes against the `memories` table, so they belong with the rest of the Memory
persistence seam.
"""
