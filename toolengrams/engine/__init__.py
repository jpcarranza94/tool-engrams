"""Engine adapters — swappable headless runners for background LLM work.

See interface.py for the contract, selection.py for how the active engine
is chosen, and claude_code.py for the first adapter.
"""

from .interface import EngineAdapter, EngineRequest, SandboxSpec
from .result import EngineResult
from .selection import ENGINES, get_engine

__all__ = [
    "ENGINES",
    "EngineAdapter",
    "EngineRequest",
    "EngineResult",
    "SandboxSpec",
    "get_engine",
]
