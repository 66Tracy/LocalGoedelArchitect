"""Synthesizer agent — produces a canonical proof body for a benchmark problem."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from local_goedel.agents.agent import Agent, AgentConfig, AgentRun
from local_goedel.agents.prompts import SYNTHESIZER_SYSTEM_PROMPT
from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.canonical import (
    CanonicalProblem,
    axiom_whitelist_ok,
    build_canonical_submission,
    scan_proof_body,
)
from local_goedel.domain.lean_check import CheckResult
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus
from local_goedel.logging_utils import get_logger
from local_goedel.tools.base import ToolContext, ToolRegistry
from local_goedel.tools.lean_compile import LeanCompileTool
from local_goedel.tools.mathlib_search import MathlibSearchTool

if TYPE_CHECKING:
    from local_goedel.clients.lean_client import LeanClient
    from local_goedel.clients.llm_client import LLMClient
    from local_goedel.clients.mathlib_client import MathlibSearchClient


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass
class ProvedLemma:
    """A previously proved helper lemma available as reference."""
    lean_name: str
    signature: str
    proof: str


@dataclass
class SynthesisResult:
    """Result from Synthesizer.synthesize."""
    success: bool
    proof_body: Optional[str] = None
    check: Optional[CheckResult] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

class Synthesizer:
    """Canonical prover: drives an LLM agent to produce a proof body that
    passes the anti-cheat canonical gate (§C.2).

    The synthesizer is reusable: it accepts an explicit CanonicalProblem,
    optional pre-proved lemmas for context, and an optional blueprint sketch.
    It is independent of the per-node blueprint machinery.
    """

    def __init__(
        self,
        llm: "LLMClient",
        registry: ToolRegistry,
        max_turns: int = 60,
        max_tool_calls: int = 40,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_turns = max_turns
        self._max_tool_calls = max_tool_calls
        self._logger = logger or get_logger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def synthesize(
        self,
        canonical: CanonicalProblem,
        proved_lemmas: list[ProvedLemma],
        sketch: str,
        lean_client: Any,
        mathlib_client: Any,
        max_tool_calls: Optional[int] = None,
        allowed_tools: Optional[set[str]] = None,
    ) -> SynthesisResult:
        """Attempt to synthesize a canonical proof body.

        Args:
            canonical:      The CanonicalProblem specifying header, pre_decls,
                            thm_name, thm_signature, keyword.
            proved_lemmas:  Previously proved helper lemmas available as
                            reference. May be empty (prove from scratch).
            sketch:         A natural language proof sketch; may be empty.
            lean_client:    Live Lean server client (must have .check method).
            mathlib_client: Mathlib search client.
            max_tool_calls: Override the instance-level tool call budget.

        Returns:
            SynthesisResult with success=True and proof_body/check set if a
            canonical proof was found and independently verified.
        """
        effective_max_calls = max_tool_calls if max_tool_calls is not None else self._max_tool_calls

        # Build a synthetic BlueprintNode for ToolContext (required by base
        # infrastructure, but we override the canonical path via ctx.canonical).
        dummy_node = BlueprintNode(
            id="_synthesizer_target",
            lean_name=canonical.thm_name,
            signature=canonical.thm_signature,
            kind=NodeKind.TARGET,
            status=NodeStatus.PENDING,
        )

        ctx = ToolContext(
            lean_client=lean_client,
            mathlib_client=mathlib_client,
            target_node=dummy_node,
            parent_nodes=[],
            settings=None,
            logger=self._logger,
            canonical=canonical,
        )

        user_prompt = self._build_user_prompt(canonical, proved_lemmas, sketch)

        config = AgentConfig(
            max_turns=self._max_turns,
            max_tool_calls=effective_max_calls,
            system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
            allowed_tools=allowed_tools,
        )

        agent = Agent(
            llm_client=self._llm,
            registry=self._registry,
            config=config,
            logger=self._logger,
        )

        run: AgentRun = agent.run(user_prompt, ctx)

        # Scan tool messages for the first PROOF COMPLETE result.
        best_body: Optional[str] = None
        for i, msg in enumerate(run.messages):
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if "PROOF COMPLETE" in content and "canonical" in content:
                    body = self._extract_proof_body_from_messages(run.messages, i)
                    if body is not None:
                        best_body = body
                        break  # take first confirmed success

        if best_body is None:
            return SynthesisResult(
                success=False,
                reason=f"Agent stopped without PROOF COMPLETE (stop_reason={run.stop_reason})",
            )

        # Independent final verification.
        return self._final_verify(canonical, best_body, lean_client)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_user_prompt(
        self,
        canonical: CanonicalProblem,
        proved_lemmas: list[ProvedLemma],
        sketch: str,
    ) -> str:
        """Construct the user-facing prompt."""
        # Reconstruct canonical statement for display (with sorry body as placeholder).
        parts: list[str] = []
        parts.append("## Canonical Statement")
        parts.append("```lean")
        parts.append(canonical.header)
        if canonical.pre_decls:
            parts.append("")
            parts.append(canonical.pre_decls)
        parts.append("")
        parts.append(
            f"{canonical.keyword} {canonical.thm_name} {canonical.thm_signature} := by sorry"
        )
        parts.append("```")
        parts.append("")
        parts.append("Prove the theorem above (no sorry allowed).")
        parts.append("")

        if proved_lemmas:
            parts.append("## Pre-proved helper lemmas (for reference only)")
            parts.append(
                "You may inline any of these as `have` inside your proof. "
                "Do NOT emit them as top-level declarations."
            )
            parts.append("```lean")
            for lem in proved_lemmas:
                parts.append(f"-- {lem.lean_name}")
                parts.append(f"theorem {lem.lean_name} {lem.signature} :=")
                # Indent proof body for readability.
                for line in lem.proof.splitlines():
                    parts.append("  " + line if line.strip() else line)
                parts.append("")
            parts.append("```")
            parts.append("")
        else:
            parts.append("No pre-proved lemmas available — prove from scratch.")
            parts.append("")

        if sketch:
            parts.append("## Proof sketch")
            parts.append(sketch)
            parts.append("")

        parts.append(
            "Submit ONLY the proof body (the part after ':=') using "
            "`lean_compile` with mode='canonical'. "
            "Iterate until you see 'PROOF COMPLETE: sorry-free (canonical, axioms OK)'."
        )

        return "\n".join(parts)

    def _extract_proof_body_from_messages(
        self,
        messages: list[dict[str, Any]],
        tool_result_idx: int,
    ) -> Optional[str]:
        """Find the proof body from the tool call that produced a PROOF COMPLETE result."""
        for i in range(tool_result_idx - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if fn.get("name") == "lean_compile":
                        try:
                            a = json.loads(fn.get("arguments", "{}"))
                            if a.get("mode") == "canonical":
                                return a.get("code")
                        except Exception:
                            return None
                break
        return None

    def _final_verify(
        self,
        canonical: CanonicalProblem,
        body: str,
        lean_client: Any,
    ) -> SynthesisResult:
        """Independent final verification of the candidate proof body."""
        self._logger.info(
            "Synthesizer: running final verification for %s", canonical.thm_name
        )

        # Body scan first (fast).
        violations = scan_proof_body(body)
        if violations:
            return SynthesisResult(
                success=False,
                proof_body=body,
                reason="Final body scan violations: " + "; ".join(violations),
            )

        final_code = build_canonical_submission(canonical, body, add_axiom_print=True)

        try:
            from local_goedel.clients.lean_client import LeanServerError
            check = lean_client.check(final_code)
        except Exception as e:
            return SynthesisResult(
                success=False,
                proof_body=body,
                reason=f"Final verification Lean call failed: {e}",
            )

        if check.has_errors:
            error_lines = [
                f"[L{e.pos.get('line','?')}:C{e.pos.get('column','?')}] {e.data}"
                for e in check.errors[:5]
            ]
            return SynthesisResult(
                success=False,
                proof_body=body,
                check=check,
                reason="Final check errors: " + "; ".join(error_lines),
            )

        if uses_sorry(check, canonical.thm_name):
            return SynthesisResult(
                success=False,
                proof_body=body,
                check=check,
                reason="Final check: proof uses sorry",
            )

        ok_ax, offending = axiom_whitelist_ok(check, canonical.thm_name)
        if not ok_ax:
            return SynthesisResult(
                success=False,
                proof_body=body,
                check=check,
                reason=f"Final check: disallowed axioms {offending}",
            )

        self._logger.info("Synthesizer: final verification SUCCESS for %s", canonical.thm_name)
        return SynthesisResult(
            success=True,
            proof_body=body,
            check=check,
            reason="Proved and verified (canonical, axioms OK)",
        )
