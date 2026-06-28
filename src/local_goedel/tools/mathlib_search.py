"""Mathlib search tool."""
from __future__ import annotations

from typing import Any

from local_goedel.tools.base import ToolContext, ToolResult


class MathlibSearchTool:
    name = "mathlib_search"
    schema = {
        "description": (
            "Search Mathlib for specific lemma names and signatures to use in a proof "
            "(returns at most 5 results). Use for targeted Mathlib lookups, not to find the proof itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language or Lean term to search for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (1-5, default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = args.get("query", "")
        limit = max(1, min(int(args.get("limit", 5)), 5))

        if not query.strip():
            return ToolResult(ok=False, content="Query cannot be empty.")

        hits = ctx.mathlib_client.search(query, limit=limit)
        if not hits:
            return ToolResult(
                ok=True,
                content="No results found.",
                data=[],
            )

        lines = []
        for hit in hits:
            doc_short = (hit.docstring or "")[:120]
            lines.append(f"{hit.lean_name} : {hit.signature_or_source}")
            if doc_short:
                lines.append(f"  {doc_short}")

        return ToolResult(ok=True, content="\n".join(lines), data=hits)
