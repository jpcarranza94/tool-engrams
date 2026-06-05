"""Reinforcement: how a memory's score evolves over its lifetime.

  - scoring: the formula (usefulness × recency × pinning) applied to candidates.

The side effects that move the inputs to that formula (surface_count,
useful_count, soft-demote, archive, restore) live in `memory_store` — they are
writes against the `memories` table, so they belong with the rest of the Memory
persistence seam.
"""
