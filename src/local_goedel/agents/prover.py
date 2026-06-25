"""Prover agent - drives the LLM to prove a single theorem."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from local_goedel.agents.agent import Agent, AgentConfig, AgentRun
from local_goedel.agents.prompts import DIAGNOSIS_PROMPT, PROVER_SYSTEM_PROMPT
from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.lean_assembler import build_node_file
from local_goedel.domain.lean_check import CheckResult
from local_goedel.domain.node import BlueprintNode
from local_goedel.domain.results import (
    Diagnosis,
    DiagnosisKind,
    ProofResult,
    ProposedLemma,
)
from local_goedel.logging_utils import get_logger
from local_goedel.tools.base import ToolContext, ToolRegistry
from local_goedel.tools.lean_compile import LeanCompileTool
from local_goedel.tools.mathlib_search import MathlibSearchTool

if TYPE_CHECKING:
    from local_goedel.clients.lean_client import LeanClient
    from local_goedel.clients.llm_client import LLMClient
    from local_goedel.clients.mathlib_client import MathlibSearchClient
    from local_goedel.config import Settings


def _extract_json_object(text: str) -> Optional[str]:
    """Extract the first JSON object from text."""
    import re
    fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate.startswith('{'):
            return candidate

    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


class Prover:
    def __init__(
        self,
        llm: "LLMClient",
        registry: ToolRegistry,
        settings: "Settings",
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._settings = settings
        self._logger = get_logger(__name__)

    def prove(
        self,
        target_node: BlueprintNode,
        parent_nodes: list[BlueprintNode],
        iteration: int = 0,
    ) -> ProofResult:
        """Attempt to prove the target node."""
        self._logger.info(
            "Prover.prove: %s (iteration=%d)", target_node.lean_name, iteration
        )
        ctx = ToolContext(
            lean_client=None,
            mathlib_client=None,
            target_node=target_node,
            parent_nodes=parent_nodes,
            settings=self._settings,
            logger=self._logger,
        )
        return self._prove_with_ctx(target_node, parent_nodes, ctx, iteration)

    def prove_with_clients(
        self,
        target_node: BlueprintNode,
        parent_nodes: list[BlueprintNode],
        lean_client: Any,
        mathlib_client: Any,
        iteration: int = 0,
    ) -> ProofResult:
        """Prove with explicit clients."""
        ctx = ToolContext(
            lean_client=lean_client,
            mathlib_client=mathlib_client,
            target_node=target_node,
            parent_nodes=parent_nodes,
            settings=self._settings,
            logger=self._logger,
        )
        return self._prove_with_ctx(target_node, parent_nodes, ctx, iteration)

    def _prove_with_ctx(
        self,
        target_node: BlueprintNode,
        parent_nodes: list[BlueprintNode],
        ctx: ToolContext,
        iteration: int,
    ) -> ProofResult:
        # Assemble skeleton for the user prompt
        skeleton = build_node_file(
            target_node, parent_nodes, proof_body="by sorry", add_axiom_print=False
        )

        user_prompt = (
            f"Prove the following Lean 4 theorem (no sorry allowed):\n\n"
            f"```lean\n{skeleton}\n```\n\n"
            f"Natural language statement: {target_node.nl_statement or '(none provided)'}\n\n"
            f"Proof sketch: {target_node.proof_sketch or '(none provided)'}\n\n"
            f"Use lean_compile with mode='proof_body' to test proof bodies. "
            f"Keep iterating until you see 'PROOF COMPLETE: sorry-free'."
        )

        config = AgentConfig(
            max_turns=self._settings.prover_max_turns,
            max_tool_calls=self._settings.prover_max_tool_calls,
            system_prompt=PROVER_SYSTEM_PROMPT,
        )

        agent = Agent(
            llm_client=self._llm,
            registry=self._registry,
            config=config,
            logger=self._logger,
        )

        run: AgentRun = agent.run(user_prompt, ctx)

        # Find best successful check from the run
        best_check: Optional[CheckResult] = None
        best_proof_body: Optional[str] = None

        # Scan tool results in messages for successful compilations
        for i, msg in enumerate(run.messages):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if "PROOF COMPLETE: sorry-free" in content:
                    proof_body = self._extract_proof_body_from_messages(
                        run.messages, i
                    )
                    if proof_body is not None:
                        best_proof_body = proof_body
                        if ctx.last_check is not None:
                            best_check = ctx.last_check

        # Attempt final independent verification
        if best_proof_body is not None and ctx.lean_client is not None:
            self._logger.info("Running final independent verification...")
            final_code = build_node_file(
                target_node,
                parent_nodes,
                proof_body=best_proof_body,
                add_axiom_print=True,
            )
            try:
                final_check = ctx.lean_client.check(final_code)
                if not final_check.has_errors and not uses_sorry(
                    final_check, target_node.lean_name
                ):
                    self._logger.info("Final verification: SUCCESS")
                    return ProofResult(
                        success=True,
                        proof_body=best_proof_body,
                        check_result=final_check,
                        diagnosis=Diagnosis(
                            kind=DiagnosisKind.SUCCESS,
                            message="Proved and verified.",
                            check_result=final_check,
                        ),
                        iterations=iteration,
                    )
                else:
                    self._logger.warning(
                        "Final verification FAILED (has_errors=%s sorry=%s)",
                        final_check.has_errors,
                        uses_sorry(final_check, target_node.lean_name),
                    )
            except Exception as e:
                self._logger.error("Final verification error: %s", e)

        # Run diagnosis on failure
        diagnosis = self._run_diagnosis(run, target_node, ctx)

        return ProofResult(
            success=False,
            proof_body=best_proof_body,
            check_result=best_check or ctx.last_check,
            diagnosis=diagnosis,
            iterations=iteration,
        )

    def _run_diagnosis(
        self,
        run: AgentRun,
        target_node: BlueprintNode,
        ctx: ToolContext,
    ) -> Diagnosis:
        """Run a diagnosis LLM call to determine why the proof failed."""
        self._logger.info(
            "Running diagnosis for failed node: %s", target_node.lean_name
        )

        # Build a summary of the failed proof attempt
        transcript_summary = self._summarize_transcript(run)

        diagnosis_user = (
            f"The prover agent failed to prove:\n\n"
            f"```lean\ntheorem {target_node.lean_name} {target_node.signature}\n```\n\n"
            f"Natural language: {target_node.nl_statement or '(none)'}\n\n"
            f"## Proof attempt transcript summary\n{transcript_summary}\n\n"
            f"Emit a JSON diagnosis verdict now."
        )

        messages = [
            {"role": "system", "content": DIAGNOSIS_PROMPT},
            {"role": "user", "content": diagnosis_user},
        ]

        try:
            msg = self._llm.chat(
                messages=messages,
                tools=None,
            )
            content = ""
            if isinstance(getattr(msg, "content", None), str):
                content = msg.content or ""
            elif isinstance(getattr(msg, "content", None), list):
                parts = []
                for p in msg.content:
                    if hasattr(p, "type") and getattr(p, "type") == "text":
                        parts.append(getattr(p, "text", ""))
                content = " ".join(parts)

            return self._parse_diagnosis(content, target_node)
        except Exception as e:
            self._logger.warning("Diagnosis LLM call failed: %s", e)
            return Diagnosis(
                kind=DiagnosisKind.UNKNOWN,
                message=f"Diagnosis failed: {e}",
                analysis=f"Agent ended with stop_reason={run.stop_reason}",
            )

    def _summarize_transcript(self, run: AgentRun) -> str:
        """Build a concise transcript summary for diagnosis."""
        lines = []
        tool_call_count = 0
        for msg in run.messages:
            role = msg.get("role", "")
            if role == "tool":
                content = msg.get("content", "")
                tool_call_count += 1
                # Only include error messages and last few
                if "error" in content.lower() or "sorry" in content.lower():
                    lines.append(f"[Tool result {tool_call_count}]: {content[:300]}")

        # Include last assistant message
        for msg in reversed(run.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "") or ""
                if isinstance(content, str) and content:
                    lines.append(f"[Final LLM text]: {content[:500]}")
                break

        lines.append(f"[Stop reason]: {run.stop_reason}")
        lines.append(f"[Total tool calls]: {run.tool_call_count}")
        return "\n".join(lines[-20:])  # last 20 lines

    def _parse_diagnosis(self, content: str, target_node: BlueprintNode) -> Diagnosis:
        """Parse diagnosis JSON from LLM content."""
        json_str = _extract_json_object(content)
        if not json_str:
            return Diagnosis(
                kind=DiagnosisKind.UNKNOWN,
                message="Could not parse diagnosis JSON",
                analysis=content[:300],
            )

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return Diagnosis(
                kind=DiagnosisKind.UNKNOWN,
                message="Invalid diagnosis JSON",
                analysis=content[:300],
            )

        kind_str = data.get("kind", "UNKNOWN").upper()
        if kind_str == "STATEMENT_WRONG":
            kind = DiagnosisKind.STATEMENT_WRONG
        elif kind_str == "PROOF_TOO_HARD":
            kind = DiagnosisKind.PROOF_TOO_HARD
        else:
            kind = DiagnosisKind.UNKNOWN

        helpers = []
        for h in data.get("suggested_helpers", []):
            if isinstance(h, dict):
                helpers.append(ProposedLemma(
                    lean_name=h.get("lean_name", ""),
                    signature=h.get("signature", ""),
                    nl_statement=h.get("nl_statement", ""),
                    proof_sketch=h.get("proof_sketch", ""),
                    parents=h.get("parents", []),
                ))

        return Diagnosis(
            kind=kind,
            message=data.get("analysis", "")[:500],
            analysis=data.get("analysis", ""),
            suggested_fix=data.get("suggested_fix", ""),
            suggested_helpers=helpers,
        )

    def _extract_proof_body_from_messages(
        self,
        messages: list[dict[str, Any]],
        tool_result_idx: int,
    ) -> Optional[str]:
        """Find the proof body from the tool call that produced a result."""
        for i in range(tool_result_idx - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if fn.get("name") == "lean_compile":
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                            return args.get("code")
                        except Exception:
                            return None
                break
        return None
