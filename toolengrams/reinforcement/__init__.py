"""Reinforcement: how a memory's score evolves over its lifetime.

Two concerns:
  - scoring:  the formula (usefulness × recency × pinning) applied to candidates
  - counters: the side effects that move the inputs to that formula —
              surface_count, useful_count, soft-demote, archive, restore
"""
