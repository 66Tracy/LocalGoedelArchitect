"""Lean compile tool."""
from __future__ import annotations

from typing import Any

from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.lean_assembler import build_node_file
from local_goedel.clients.lean_client import LeanServerError
from local_goedel.tools.base import ToolContext, ToolResult


class LeanCompileTool:
    name = "lean_compile"
    schema = {
        "description": (
            "Compile Lean 4 code and return errors/warnings. "
            "mode='proof_body': submit only the proof body (after ':= '), the system "
            "assembles the full file. "
            "mode='full_file': submit a complete Lean file verbatim."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Lean code to check.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["proof_body", "full_file"],
                    "description": "How to interpret the code argument.",
                    "default": "proof_body",
                },
            },
            "required": ["code"],
        },
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = args.get("code", "")
        mode = args.get("mode", "proof_body")

        if mode == "proof_body":
            lean_code = build_node_file(
                ctx.target_node,
                ctx.parent_nodes,
                proof_body=code,
                add_axiom_print=True,
            )
        else:
            lean_code = code

        ctx.attempts += 1
        ctx.logger.debug("lean_compile (mode=%s) attempt=%d", mode, ctx.attempts)

        try:
            check = ctx.lean_client.check(lean_code)
        except LeanServerError as e:
            return ToolResult(ok=False, content=f"Lean server error: {e}")

        ctx.last_check = check

        if not check.has_errors:
            # Check axioms
            sorry_free = not uses_sorry(check, ctx.target_node.lean_name)
            if sorry_free:
                return ToolResult(
                    ok=True,
                    content="PROOF COMPLETE: sorry-free",
                    data=check,
                )
            else:
                # Compiled but uses sorry
                sorry_msgs = [
                    m.data for m in check.messages
                    if m.severity == "warning" and "sorry" in m.data.lower()
                ]
                summary = "Compiled OK but uses sorry. " + "; ".join(sorry_msgs[:3])
                return ToolResult(ok=True, content=summary, data=check)

        # Has errors - summarize them
        error_lines = []
        for err in check.errors[:10]:
            pos = err.pos
            loc = f"L{pos.get('line', '?')}:C{pos.get('column', '?')}"
            error_lines.append(f"[{loc}] {err.data}")

        sorry_note = ""
        if check.mentions_sorry():
            sorry_note = " (also uses sorry)"

        summary = f"{len(check.errors)} error(s){sorry_note}:\n" + "\n".join(error_lines)
        return ToolResult(ok=False, content=summary, data=check)
