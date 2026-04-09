"""Memory interface and v0.1 stub.

The Memory interface exists in v0.1 as a no-op so the agent loop can take
it as a parameter without special-casing. v0.5 will plug in a real
implementation (likely NanoGraph) behind this same interface, and the
agent loop will not need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Recollection:
    """A single fact recalled from memory."""

    text: str
    score: float
    source: str | None = None


class Memory(Protocol):
    """Persistent semantic memory across tasks. v0.1 is a no-op stub."""

    def remember(self, fact: str, source: str | None = None) -> None: ...
    def recall(self, query: str, k: int = 5) -> list[Recollection]: ...


class NoMemory:
    """Stub implementation. Does nothing, returns nothing.

    The agent loop calls these methods unconditionally, so v0.1 plugs in
    NoMemory and the loop's recall paths are simply empty. v0.5 swaps in
    a NanoGraph-backed Memory and the loop starts producing different
    behavior with no code change.
    """

    def remember(self, fact: str, source: str | None = None) -> None:
        return None

    def recall(self, query: str, k: int = 5) -> list[Recollection]:
        return []
