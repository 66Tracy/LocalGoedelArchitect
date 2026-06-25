"""Tool infrastructure."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from local_goedel.assembly.canonical import CanonicalProblem
from local_goedel.domain.lean_check import CheckResult
from local_goedel.domain.node import BlueprintNode
from local_goedel.telemetry import get_telemetry


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: Optional[Any] = None


class Tool(Protocol):
    name: str
    schema: dict[str, Any]

    def run(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult:
        ...


@dataclass
class ToolContext:
    lean_client: Any
    mathlib_client: Any
    target_node: BlueprintNode
    parent_nodes: list[BlueprintNode]
    settings: Any
    logger: logging.Logger
    attempts: int = 0
    last_check: Optional[CheckResult] = None
    canonical: Optional[CanonicalProblem] = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def schemas(self, allowed: Optional[set[str]] = None) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas.

        Args:
            allowed: If provided, only include tools whose name is in this set.
                     When None (default), all registered tools are returned.
        """
        result = []
        for tool in self._tools.values():
            if allowed is not None and tool.name not in allowed:
                continue
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.schema.get("description", ""),
                    "parameters": tool.schema.get("parameters", {}),
                },
            })
        return result

    def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Dispatch a tool call by name."""
        tel = get_telemetry()
        if tel is not None:
            tel.add_tool_call(name)
        if name not in self._tools:
            return ToolResult(ok=False, content=f"Unknown tool: {name}")
        return self._tools[name].run(args, ctx)
