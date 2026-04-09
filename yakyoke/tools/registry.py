"""Tool registry.

Tools are uniform: each is a Python function with signature
    (workspace: Path, **kwargs) -> str
and a JSON schema describing its parameters.

Tools must be process-safe: no module-level mutable state, no globals.
Filesystem operations write only inside the per-task workspace. This is
what makes parallel workers safe in v0.2 with zero changes here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# A tool function takes a workspace dir + kwargs from the model and returns
# a string. Errors are returned as strings, not raised, so the model can
# react and try again.
ToolFunc = Callable[..., str]


@dataclass
class ToolSpec:
    """A tool's callable plus its OpenAI-format schema for the LLM."""

    name: str
    func: ToolFunc
    schema: dict[str, Any]  # OpenAI tools[] entry


class ToolRegistry:
    """Holds tools and produces the schemas list for an LLM call.

    The agent loop never imports tools directly. The worker assembles a
    registry at startup and passes it to the loop. This makes per-task
    tool allowlists trivial: filter the registry by the task's tools[]
    field before passing it in.
    """

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-format tool schemas for the LLM."""
        return [t.schema for t in self._tools.values()]

    def filtered(self, allowed: list[str]) -> "ToolRegistry":
        """Return a new registry containing only the named tools.

        If `allowed` is empty, returns a copy of self (all tools allowed).
        """
        if not allowed:
            return self
        sub = ToolRegistry()
        for name in allowed:
            spec = self._tools.get(name)
            if spec is not None:
                sub.register(spec)
        return sub


def build_default_registry() -> ToolRegistry:
    """Assemble the v0.1 tool set.

    Imported here (not at module load) so the daemon can boot even if
    optional tool dependencies are missing.
    """
    from yakyoke.tools.web import fetch_url_spec, web_search_spec
    from yakyoke.tools.filesystem import (
        filesystem_read_spec,
        filesystem_write_spec,
        filesystem_list_spec,
    )

    reg = ToolRegistry()
    reg.register(web_search_spec())
    reg.register(fetch_url_spec())
    reg.register(filesystem_write_spec())
    reg.register(filesystem_read_spec())
    reg.register(filesystem_list_spec())
    return reg
