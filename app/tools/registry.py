"""
Tool registry — registers callable handlers that Claude can invoke via tool_use.

Each tool is a ToolSpec: name, description, JSONSchema input, sync callable.
The registry exposes the list of specs as Anthropic API `tools=[...]` payload
and dispatches tool_use blocks to handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import structlog

log = structlog.get_logger()


class ToolError(Exception):
    """Tool handler raised a recoverable error. The message is returned to Claude."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    """Handler returns JSON-serialisable content OR a list of Anthropic-shape
    content blocks (e.g. [{"type":"text","text":"..."}, {"type":"image", ...}])."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec
        log.info("tool.registered", name=spec.name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def as_anthropic_tools(self) -> list[dict]:
        """Render as the Anthropic API `tools` list."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.input_schema,
            }
            for s in self._tools.values()
        ]

    def invoke(self, name: str, arguments: dict) -> Any:
        if name not in self._tools:
            raise ToolError(f"unknown tool: {name}")
        handler = self._tools[name].handler
        log.info("tool.invoke", name=name, args_keys=list(arguments.keys()))
        try:
            return handler(arguments)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            # Convert unexpected errors to ToolError so Claude gets a
            # structured message rather than the request failing entirely.
            log.exception("tool.error", name=name)
            raise ToolError(f"internal error in tool {name!r}: {e}") from e
