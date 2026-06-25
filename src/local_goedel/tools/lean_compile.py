"""Lean compile tool."""
from __future__ import annotations

from typing import Any

from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.canonical import (
    axiom_whitelist_ok,
    build_canonical_submission,
    scan_proof_body,
)
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
                    "enum": ["proof_body", "full_file", "canonical"],
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

        if mode == "canonical":
            return self._run_canonical(code, ctx)

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

        # Branch on status when available (kimina path); fall back to
        # has_errors / mentions_sorry when status is empty (HTTP path).
        status = check.status

        if status == "timeout_error":
            return ToolResult(ok=False, content="Lean check timed out.", data=check)

        if status == "valid" or (status == "" and not check.has_errors):
            # Check axioms — valid status alone does not exclude native_decide/extra axioms.
            sorry_free = not uses_sorry(check, ctx.target_node.lean_name)
            if sorry_free:
                return ToolResult(
                    ok=True,
                    content="PROOF COMPLETE: sorry-free",
                    data=check,
                )
            else:
                # Compiled clean but uses sorry (axiom check caught it)
                sorry_msgs = [
                    m.data for m in check.messages
                    if m.severity == "warning" and "sorry" in m.data.lower()
                ]
                summary = "Compiled OK but uses sorry. " + "; ".join(sorry_msgs[:3])
                return ToolResult(ok=True, content=summary, data=check)

        if status == "sorry":
            # Compiled but proof uses sorry
            sorry_msgs = [
                m.data for m in check.messages
                if m.severity == "warning" and "sorry" in m.data.lower()
            ]
            if not sorry_msgs and check.sorries:
                # Build a message from the first sorry goal
                first = check.sorries[0]
                goal = first.get("goal", "") if isinstance(first, dict) else ""
                sorry_msgs = [f"sorry goal: {goal}"] if goal else ["(sorry goal unavailable)"]
            summary = "Compiled OK but uses sorry. " + "; ".join(sorry_msgs[:3])
            return ToolResult(ok=True, content=summary, data=check)

        # lean_error or status=="" with errors
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

    def _run_canonical(self, code: str, ctx: ToolContext) -> ToolResult:
        """Handle mode='canonical': anti-cheat gate for canonical submission."""
        if ctx.canonical is None:
            return ToolResult(
                ok=False,
                content="canonical mode requires a canonical problem on ctx",
            )

        # Fast-path: scan proof body for violations BEFORE calling Lean.
        violations = scan_proof_body(code)
        if violations:
            return ToolResult(
                ok=False,
                content=(
                    "SAFEGUARD VIOLATION: "
                    + "; ".join(violations)
                    + ". Submit ONLY the proof body; put helper lemmas inside as `have`; "
                    "no axiom/native_decide/import/open."
                ),
            )

        lean_code = build_canonical_submission(ctx.canonical, code, add_axiom_print=True)

        ctx.attempts += 1
        ctx.logger.debug("lean_compile (mode=canonical) attempt=%d", ctx.attempts)

        try:
            check = ctx.lean_client.check(lean_code)
        except LeanServerError as e:
            return ToolResult(ok=False, content=f"Lean server error: {e}")

        ctx.last_check = check
        status = check.status
        thm_name = ctx.canonical.thm_name

        if status == "timeout_error":
            return ToolResult(ok=False, content="Lean check timed out.", data=check)

        if status in ("lean_error", "") and check.has_errors:
            error_lines = []
            for err in check.errors[:10]:
                pos = err.pos
                loc = f"L{pos.get('line', '?')}:C{pos.get('column', '?')}"
                error_lines.append(f"[{loc}] {err.data}")
            sorry_note = " (also uses sorry)" if check.mentions_sorry() else ""
            summary = f"{len(check.errors)} error(s){sorry_note}:\n" + "\n".join(error_lines)
            return ToolResult(ok=False, content=summary, data=check)

        if status == "sorry" or uses_sorry(check, thm_name):
            return ToolResult(
                ok=False,
                content="Compiled but uses sorry — not accepted.",
                data=check,
            )

        # status is "valid" (or "" with no errors) and not sorry — check axioms.
        ok_ax, offending = axiom_whitelist_ok(check, thm_name)
        if ok_ax:
            return ToolResult(
                ok=True,
                content="PROOF COMPLETE: sorry-free (canonical, axioms OK)",
                data=check,
            )
        else:
            return ToolResult(
                ok=False,
                content=(
                    "AXIOM VIOLATION: proof depends on disallowed axioms "
                    + str(offending)
                    + " — remove native_decide/axiom."
                ),
                data=check,
            )
