"""Main pipeline orchestrating generate -> prove -> refine loop."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from local_goedel.agents.blueprint_generator import BlueprintGenerator
from local_goedel.agents.blueprint_refiner import BlueprintRefiner
from local_goedel.agents.prompts import ONESHOT_SYSTEM_PROMPT, SYNTHESIZER_SYSTEM_PROMPT
from local_goedel.agents.prover import Prover
from local_goedel.agents.synthesizer import ProvedLemma, Synthesizer
from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.canonical import (
    CanonicalProblem,
    axiom_whitelist_ok,
    build_canonical_submission,
    scan_proof_body,
    split_canonical,
)
from local_goedel.assembly.lean_assembler import build_final_file, parse_theorem_file
from local_goedel.clients.lean_client import LeanClient
from local_goedel.clients.llm_client import LLMClient
from local_goedel.clients.mathlib_client import MathlibSearchClient
from local_goedel.config import validate_mode
from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import NodeKind, NodeStatus
from local_goedel.logging_utils import current_run_logfile, get_logger
from local_goedel.orchestrator.artifacts import ArtifactWriter, make_run_id
from local_goedel.orchestrator.scheduler import prove_wave
from local_goedel.orchestrator.state import IterationRecord, RunOutcome
from local_goedel.tools.base import ToolRegistry
from local_goedel.tools.lean_compile import LeanCompileTool
from local_goedel.tools.mathlib_search import MathlibSearchTool

if TYPE_CHECKING:
    from local_goedel.config import Settings


class Pipeline:
    """Full generate -> prove -> refine pipeline with ablation mode support."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._logger = get_logger(__name__)

        # Initialize clients
        self._lean_client = LeanClient(settings)
        self._mathlib_client = MathlibSearchClient(settings)
        self._llm_client = LLMClient(settings)

        # Initialize tools + prover + synthesizer
        self._registry = ToolRegistry()
        self._registry.register(LeanCompileTool())
        self._registry.register(MathlibSearchTool())
        self._prover = Prover(self._llm_client, self._registry, settings)
        self._synthesizer = Synthesizer(
            llm=self._llm_client,
            registry=self._registry,
            max_turns=settings.prover_max_turns,
            max_tool_calls=settings.prover_max_tool_calls,
        )

        # Generator + refiner (used only by 'full' mode)
        self._generator = BlueprintGenerator(
            llm=self._llm_client,
            lean_client=self._lean_client,
            settings=settings,
        )
        self._refiner = BlueprintRefiner(
            llm=self._llm_client,
            lean_client=self._lean_client,
            settings=settings,
        )

    def run(
        self,
        theorem_file_path: Optional[Path | str] = None,
        difficulty: str = "easy",
        max_iter: Optional[int] = None,
        canonical: Optional[CanonicalProblem] = None,
        mode: Optional[str] = None,
        theorem_src: Optional[str] = None,
    ) -> RunOutcome:
        """Run the pipeline in the configured mode.

        Args:
            theorem_file_path: Path to the .lean theorem file. Optional when
                theorem_src is provided directly.
            difficulty: "easy" or "hard" (used by 'full' mode only)
            max_iter: Override max iterations (None = use settings; 'full' only)
            canonical: Optional pre-built CanonicalProblem. If None, derived
                from theorem_src via split_canonical.
            mode: Override the mode from settings ("full", "tool_loop", "oneshot").
            theorem_src: Optional Lean source string. When provided, the file at
                theorem_file_path is NOT read (but theorem_file_path is still used
                to derive the problem name if given). Backward-compatible: when
                omitted, theorem_file_path must be provided and is read as before.

        Returns:
            RunOutcome
        """
        # Resolve and validate mode
        effective_mode = mode if mode is not None else self._settings.mode
        validate_mode(effective_mode)

        # Resolve source and problem name
        if theorem_src is not None:
            # Source provided directly (benchmark path)
            if theorem_file_path is not None:
                problem_name = Path(theorem_file_path).stem
            else:
                problem_name = "unknown_problem"
        else:
            if theorem_file_path is None:
                raise ValueError(
                    "Pipeline.run requires either theorem_file_path or theorem_src"
                )
            theorem_file_path = Path(theorem_file_path)
            theorem_src = theorem_file_path.read_text(encoding="utf-8")
            problem_name = theorem_file_path.stem

        # Build canonical problem
        if canonical is None:
            canonical = split_canonical(theorem_src)

        # Create run artifacts
        runs_dir = Path(self._settings.runs_dir)
        run_id = make_run_id(problem_name, runs_dir)
        artifacts = ArtifactWriter(run_id, runs_dir, self._logger)

        # Route all local_goedel log records for this thread to this run's file.
        # current_run_logfile is a ContextVar so each worker thread has its own
        # copy; setting it here does NOT affect concurrent threads.
        _log_token = current_run_logfile.set(str(artifacts.log_path()))

        self._logger.info(
            "Pipeline.run: %s (mode=%s, difficulty=%s)",
            problem_name, effective_mode, difficulty,
        )

        # Save config and theorem
        artifacts.save_config(self._settings)
        artifacts.save_theorem(theorem_src)

        try:
            if effective_mode == "full":
                try:
                    _, lean_name, target_signature, _ = parse_theorem_file(theorem_src)
                except ValueError:
                    lean_name = canonical.thm_name
                    target_signature = canonical.thm_signature
                outcome = self._run_full(
                    theorem_src=theorem_src,
                    lean_name=lean_name,
                    target_signature=target_signature,
                    canonical=canonical,
                    artifacts=artifacts,
                    difficulty=difficulty,
                    max_iter=max_iter,
                )
            elif effective_mode == "tool_loop":
                outcome = self._run_tool_loop(
                    canonical=canonical,
                    artifacts=artifacts,
                )
            elif effective_mode == "oneshot":
                outcome = self._run_oneshot(
                    canonical=canonical,
                    artifacts=artifacts,
                )
            else:
                # Should never reach here after validate_mode, but be safe.
                raise ValueError(f"Unknown pipeline mode: {effective_mode!r}")

            outcome.mode = effective_mode
            artifacts.save_outcome(outcome)
            return outcome
        finally:
            current_run_logfile.reset(_log_token)

    # =========================================================================
    # Private: _run_full  (original blueprint -> prove -> refine -> finalize)
    # =========================================================================

    def _run_full(
        self,
        theorem_src: str,
        lean_name: str,
        target_signature: str,
        canonical: CanonicalProblem,
        artifacts: ArtifactWriter,
        difficulty: str = "easy",
        max_iter: Optional[int] = None,
    ) -> RunOutcome:
        """Full blueprint -> prove waves -> refine -> canonical finalize."""
        start_time = time.time()

        # Determine max iterations
        if max_iter is not None:
            max_iterations = max_iter
        elif difficulty == "hard":
            max_iterations = self._settings.iters_hard
        else:
            max_iterations = self._settings.iters_easy

        # -- Step 1: Generate blueprint -------------------------------------------
        self._logger.info("Generating blueprint...")
        try:
            blueprint = self._generator.generate(theorem_src, difficulty)
        except Exception as e:
            self._logger.error("Blueprint generation failed: %s", e)
            return RunOutcome(
                success=False,
                reason=f"Blueprint generation failed: {e}",
                iterations_used=0,
            )

        # Auto-satisfy all DEFINITION nodes -- they carry real Lean bodies and
        # are not propositions to prove.  Marking them PROVED immediately
        # allows dependent lemmas to become ready without sending definitions
        # to the prover (which would fail with "type of theorem X is not a
        # proposition").
        satisfied_defs = blueprint.auto_satisfy_definitions()
        if satisfied_defs:
            self._logger.info(
                "Auto-satisfied %d definition node(s): %s", len(satisfied_defs), satisfied_defs
            )

        artifacts.save_blueprint(blueprint, 0)
        self._logger.info("Blueprint: %d nodes", len(blueprint.nodes))

        iteration_records: list[IterationRecord] = []
        unchanged_count = 0

        # -- Step 2: Prove loop ---------------------------------------------------
        for iteration in range(1, max_iterations + 1):
            # Wall-clock check
            elapsed = time.time() - start_time
            if elapsed > self._settings.max_wall_s:
                self._logger.info("Wall clock exceeded: %.0fs > %ds", elapsed, self._settings.max_wall_s)
                return RunOutcome(
                    success=False,
                    blueprint=blueprint,
                    reason=f"Wall clock exceeded ({elapsed:.0f}s > {self._settings.max_wall_s}s)",
                    iterations_used=iteration - 1,
                )

            if blueprint.is_solved():
                break

            self._logger.info("=== Iteration %d/%d ===", iteration, max_iterations)

            # Inner wave loop: keep proving as long as something gets proved
            wave_iteration_proved = 0
            wave_num = 0
            results = []
            while True:
                ready_ids = blueprint.ready_nodes()
                if not ready_ids:
                    break

                wave_num += 1
                self._logger.info("Wave %d: %d ready nodes", wave_num, len(ready_ids))

                results = prove_wave(
                    blueprint=blueprint,
                    prover=self._prover,
                    lean_client=self._lean_client,
                    mathlib_client=self._mathlib_client,
                    iteration=iteration,
                    logger=self._logger,
                )

                # Save per-node results
                for nid, result in zip(ready_ids, results):
                    artifacts.save_node_result(iteration, nid, result)

                newly_proved = sum(1 for r in results if r.success)
                wave_iteration_proved += newly_proved
                self._logger.info(
                    "Wave %d: %d/%d proved", wave_num, newly_proved, len(results)
                )

                if newly_proved == 0:
                    break  # No progress, stop inner loop

                if blueprint.is_solved():
                    break

            record = IterationRecord(
                iteration=iteration,
                ready_node_ids=list(blueprint.ready_nodes()),  # remaining after wave
                results=results,
                refined=False,
            )
            iteration_records.append(record)

            if blueprint.is_solved():
                self._logger.info("Blueprint solved at iteration %d!", iteration)
                break

            # Check if stuck
            if wave_iteration_proved == 0:
                unchanged_count += 1
            else:
                unchanged_count = 0

            # Refine
            self._logger.info("Refining blueprint (iteration %d)...", iteration)
            old_node_count = len(blueprint.nodes)

            try:
                new_blueprint = self._refiner.refine(
                    blueprint=blueprint,
                    iteration=iteration,
                    target_lean_name=lean_name,
                    target_signature=target_signature,
                )
            except Exception as e:
                self._logger.warning("Refiner failed: %s", e)
                new_blueprint = blueprint

            # Check if refiner made changes
            graph_changed = (
                len(new_blueprint.nodes) != old_node_count or
                set(new_blueprint.nodes.keys()) != set(blueprint.nodes.keys())
            )

            blueprint = new_blueprint
            # Auto-satisfy any new DEFINITION nodes introduced by the refiner
            new_satisfied = blueprint.auto_satisfy_definitions()
            if new_satisfied:
                self._logger.info(
                    "Auto-satisfied %d new definition node(s) after refine: %s",
                    len(new_satisfied), new_satisfied,
                )
            record.refined = True
            artifacts.save_blueprint(blueprint, iteration, suffix="refined")

            # STUCK detection: no progress AND refiner returned an unchanged graph
            if unchanged_count >= 2 and not graph_changed:
                self._logger.info("STUCK: no progress and refiner unchanged. Stopping.")
                return RunOutcome(
                    success=False,
                    blueprint=blueprint,
                    reason="Stuck: no progress over 2+ iterations and refiner made no changes",
                    iterations_used=iteration,
                )

        # -- Step 3: Final gate ---------------------------------------------------
        self._logger.info("Running final gate...")
        return self._finalize(
            blueprint=blueprint,
            lean_name=lean_name,
            canonical=canonical,
            artifacts=artifacts,
            iterations_used=len(iteration_records),
        )

    # =========================================================================
    # Private: _run_tool_loop  (no blueprint, no refiner -- Synthesizer only)
    # =========================================================================

    def _run_tool_loop(
        self,
        canonical: CanonicalProblem,
        artifacts: ArtifactWriter,
    ) -> RunOutcome:
        """Prove the canonical target directly using the Synthesizer tool loop.

        No blueprint is generated.  The Synthesizer runs lean_compile +
        mathlib_search in a loop against the canonical gate until it finds a
        proof or exhausts its tool-call budget.
        """
        self._logger.info(
            "_run_tool_loop: target=%s", canonical.thm_name
        )

        synth_result = self._synthesizer.synthesize(
            canonical=canonical,
            proved_lemmas=[],
            sketch="",
            lean_client=self._lean_client,
            mathlib_client=self._mathlib_client,
            max_tool_calls=self._settings.prover_max_tool_calls,
        )

        if synth_result.success and synth_result.proof_body is not None:
            final_code = build_canonical_submission(
                canonical, synth_result.proof_body, add_axiom_print=True
            )
            self._logger.info("tool_loop: SUCCESS")
            artifacts.save_final(final_code, synth_result.check)
            return RunOutcome(
                success=True,
                final_code=final_code,
                check=synth_result.check,
                reason="Proved and verified (tool_loop canonical gate)",
                iterations_used=1,
            )

        reason = synth_result.reason or "Synthesizer failed without a reason"
        self._logger.info("tool_loop: FAILED (%s)", reason)
        placeholder = build_canonical_submission(canonical, "by sorry", add_axiom_print=False)
        artifacts.save_final(placeholder)
        return RunOutcome(
            success=False,
            final_code=placeholder,
            reason=reason,
            iterations_used=1,
        )

    # =========================================================================
    # Private: _run_oneshot  (no tools, single LLM completion)
    # =========================================================================

    def _run_oneshot(
        self,
        canonical: CanonicalProblem,
        artifacts: ArtifactWriter,
    ) -> RunOutcome:
        """Attempt to prove the theorem in a single (or very few) LLM completions.

        No tools are used.  Thinking stays ON.  The model is asked to return
        only the proof body in a ```lean fenced block.  The body is then fed
        through the same canonical gate as every other mode.

        Retries (default 1, configured by settings.oneshot_retries) feed back
        the compiler error message from the previous attempt to help the model
        correct itself, but the mode is still "essentially oneshot".
        """
        self._logger.info("_run_oneshot: target=%s", canonical.thm_name)

        max_attempts = max(1, self._settings.oneshot_retries)
        last_reason = "No attempt made"

        # Build the static portion of the user prompt once.
        user_prompt_base = self._build_oneshot_user_prompt(canonical)

        prior_error: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            self._logger.info("oneshot attempt %d/%d", attempt, max_attempts)

            user_content = user_prompt_base
            if prior_error:
                user_content += (
                    f"\n\n## Compiler feedback from attempt {attempt - 1}\n"
                    f"```\n{prior_error}\n```\n"
                    "Fix the error and return the corrected proof body."
                )

            messages = [
                {"role": "system", "content": ONESHOT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]

            try:
                response = self._llm_client.chat(messages=messages)
                raw_text = _extract_text(response)
            except Exception as e:
                last_reason = f"LLM call failed: {e}"
                self._logger.warning("oneshot LLM call failed: %s", e)
                break

            body = _extract_body_from_response(raw_text)
            if body is None:
                last_reason = "Could not extract a proof body from LLM response"
                self._logger.info("oneshot: no proof body extracted (attempt %d)", attempt)
                prior_error = "No proof body found in response. Please return a ```lean block."
                continue

            self._logger.debug("oneshot body (attempt %d): %s", attempt, body[:200])

            # Scan for violations before sending to Lean
            violations = scan_proof_body(body)
            if violations:
                last_reason = "Body scan violations: " + "; ".join(violations)
                self._logger.info("oneshot scan violations: %s", violations)
                prior_error = "Proof body has violations: " + "; ".join(violations)
                continue

            # Build canonical file and check with Lean
            final_code = build_canonical_submission(canonical, body, add_axiom_print=True)
            try:
                check = self._lean_client.check(final_code)
            except Exception as e:
                last_reason = f"Lean check failed: {e}"
                self._logger.warning("oneshot Lean check exception: %s", e)
                prior_error = f"Lean server error: {e}"
                continue

            if check.has_errors:
                error_lines = [
                    f"[L{e.pos.get('line','?')}:C{e.pos.get('column','?')}] {e.data}"
                    for e in check.errors[:10]
                ]
                prior_error = "\n".join(error_lines)
                last_reason = "Lean errors: " + prior_error
                self._logger.info("oneshot Lean errors (attempt %d): %s", attempt, prior_error[:300])
                continue

            if uses_sorry(check, canonical.thm_name):
                last_reason = "Proof uses sorry"
                prior_error = "The proof depends on sorry. Do not use sorry."
                continue

            ok_ax, offending = axiom_whitelist_ok(check, canonical.thm_name)
            if not ok_ax:
                last_reason = f"Disallowed axioms: {offending}"
                prior_error = f"Disallowed axioms in proof: {offending}. Avoid native_decide and custom axioms."
                continue

            # All checks passed
            self._logger.info("oneshot: SUCCESS at attempt %d", attempt)
            artifacts.save_final(final_code, check)
            return RunOutcome(
                success=True,
                final_code=final_code,
                check=check,
                reason=f"Proved and verified (oneshot, attempt {attempt})",
                iterations_used=attempt,
            )

        self._logger.info("oneshot: FAILED -- %s", last_reason)
        placeholder = build_canonical_submission(canonical, "by sorry", add_axiom_print=False)
        artifacts.save_final(placeholder)
        return RunOutcome(
            success=False,
            final_code=placeholder,
            reason=last_reason,
            iterations_used=max_attempts,
        )

    @staticmethod
    def _build_oneshot_user_prompt(canonical: CanonicalProblem) -> str:
        """Build the user prompt for oneshot mode."""
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
        parts.append(
            "Prove the theorem above. Return ONLY the proof body (starting with `by`) "
            "inside a ```lean fenced block. No prose after the block."
        )
        return "\n".join(parts)

    # =========================================================================
    # Private: _finalize  (canonical gate for 'full' mode)
    # =========================================================================

    def _finalize(
        self,
        blueprint: Blueprint,
        lean_name: str,
        canonical: CanonicalProblem,
        artifacts: ArtifactWriter,
        iterations_used: int,
    ) -> RunOutcome:
        """Run the canonical gate to produce and verify the final proof.

        Strategy:
        1. Collect proved helper lemmas (non-target, non-definition, PROVED) in
           topological order.
        2. Try the fast path: if the target node itself is PROVED, attempt its
           proof body directly through the canonical gate.
        3. If the fast path fails or is unavailable, run Synthesizer.synthesize.
        4. Accept iff the canonical file compiles sorry-free with whitelisted axioms.
        5. Persist final.lean (canonical form) and update outcome with reason.
        """
        # --- Collect target node and proved helper lemmas ---
        target_node = blueprint.nodes.get(blueprint.target_id)

        reachable = blueprint.reachable_from_target()
        try:
            topo = blueprint.topo_sort()
        except ValueError:
            topo = list(blueprint.nodes.keys())

        proved_lemmas: list[ProvedLemma] = []
        for nid in topo:
            if nid not in reachable:
                continue
            node = blueprint.nodes[nid]
            # Skip target, definitions, and unproved nodes.
            if nid == blueprint.target_id:
                continue
            if node.kind == NodeKind.DEFINITION:
                continue
            if node.status != NodeStatus.PROVED:
                continue
            if not node.proof:
                continue
            proved_lemmas.append(
                ProvedLemma(
                    lean_name=node.lean_name,
                    signature=node.signature,
                    proof=node.proof,
                )
            )

        sketch = target_node.proof_sketch if target_node else ""

        # --- Fast path: target node already PROVED ---
        if (
            target_node is not None
            and target_node.status == NodeStatus.PROVED
            and target_node.proof
        ):
            self._logger.info("_finalize: target node is PROVED -- trying fast-path canonical gate")
            fast_result = self._try_canonical_body(
                canonical, target_node.proof, lean_name
            )
            if fast_result is not None:
                final_code, check = fast_result
                self._logger.info("FINAL GATE: fast-path SUCCESS")
                artifacts.save_final(final_code, check)
                return RunOutcome(
                    success=True,
                    final_code=final_code,
                    check=check,
                    blueprint=blueprint,
                    reason="Proved and verified (fast-path canonical gate)",
                    iterations_used=iterations_used,
                )
            self._logger.info("_finalize: fast-path did not pass canonical gate, falling through to Synthesizer")

        # --- Synthesizer path ---
        self._logger.info(
            "_finalize: running Synthesizer (%d proved helpers)", len(proved_lemmas)
        )
        synth_result = self._synthesizer.synthesize(
            canonical=canonical,
            proved_lemmas=proved_lemmas,
            sketch=sketch,
            lean_client=self._lean_client,
            mathlib_client=self._mathlib_client,
        )

        if synth_result.success and synth_result.proof_body is not None:
            final_code = build_canonical_submission(
                canonical, synth_result.proof_body, add_axiom_print=True
            )
            self._logger.info("FINAL GATE: Synthesizer SUCCESS")
            artifacts.save_final(final_code, synth_result.check)
            return RunOutcome(
                success=True,
                final_code=final_code,
                check=synth_result.check,
                blueprint=blueprint,
                reason="Proved and verified (canonical synthesizer)",
                iterations_used=iterations_used,
            )

        reason = synth_result.reason or "Synthesizer failed without a reason"
        self._logger.info("FINAL GATE: FAILED (%s)", reason)

        # Persist a rejection record so the caller can inspect.
        placeholder = build_canonical_submission(
            canonical, "by sorry", add_axiom_print=False
        )
        artifacts.save_final(placeholder)
        return RunOutcome(
            success=False,
            final_code=placeholder,
            blueprint=blueprint,
            reason=reason,
            iterations_used=iterations_used,
        )

    def _try_canonical_body(
        self,
        canonical: CanonicalProblem,
        body: str,
        lean_name: str,
    ):
        """Try a proof body through the canonical gate.

        Returns (final_code, check) on success, None on failure.
        This is used for the fast-path when the target node is already PROVED.
        """
        violations = scan_proof_body(body)
        if violations:
            self._logger.debug("Fast-path body scan violations: %s", violations)
            return None

        final_code = build_canonical_submission(canonical, body, add_axiom_print=True)
        try:
            check = self._lean_client.check(final_code)
        except Exception as e:
            self._logger.debug("Fast-path Lean check exception: %s", e)
            return None

        if check.has_errors:
            return None
        if uses_sorry(check, lean_name):
            return None
        ok_ax, offending = axiom_whitelist_ok(check, lean_name)
        if not ok_ax:
            self._logger.debug("Fast-path axiom violation: %s", offending)
            return None

        return final_code, check


# =============================================================================
# Module-level helpers for oneshot body extraction
# =============================================================================

def _extract_text(response: object) -> str:
    """Extract the text content from an LLM response message object."""
    # OpenAI-style: message.content is a string or list of content blocks.
    content = getattr(response, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Content blocks (list)
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif hasattr(block, "text"):
                texts.append(block.text or "")
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return str(content)


def _extract_body_from_response(raw_text: str) -> Optional[str]:
    """Extract the Lean 4 proof body from a raw LLM response string.

    Strategy (in order):
    1. Look for a ```lean fenced block; return its content.
    2. Look for a ``` fenced block (language-untagged); return its content.
    3. If the response contains ':= by' or ':= ' (i.e., the model returned a
       full theorem declaration), extract the part after the last ':='.
    4. If the response starts with 'by ', treat the whole response as the body.
    5. Return None if nothing useful was found.

    In all cases, if the extracted text starts with a theorem declaration line
    (theorem/lemma keyword), we strip everything up to and including ':='.
    """
    if not raw_text:
        return None

    # 1. ```lean block
    m = re.search(r'```lean\s*\n(.*?)```', raw_text, re.DOTALL)
    if m:
        block = m.group(1).strip()
        return _strip_theorem_decl(block)

    # 2. Generic ``` block
    m = re.search(r'```\s*\n(.*?)```', raw_text, re.DOTALL)
    if m:
        block = m.group(1).strip()
        return _strip_theorem_decl(block)

    # 3. No fenced block -- try to use the whole response.
    text = raw_text.strip()
    return _strip_theorem_decl(text) if text else None


def _strip_theorem_decl(text: str) -> Optional[str]:
    """If text is a full theorem declaration, return only the proof body.

    If the text contains 'theorem NAME ... :=' or 'lemma NAME ... :=', strip
    everything up to and including the last ':=' (so we keep only the body).
    If the text does not look like a declaration, return it unchanged.
    If the result is empty, return None.
    """
    if not text:
        return None

    # Check if there's a top-level theorem/lemma declaration.
    decl_match = re.search(r'^(?:theorem|lemma)\s+\S+', text, re.MULTILINE)
    if decl_match:
        # Find the ':=' that ends the declaration (the one on the declaration
        # line or immediately after it, not := inside the proof body like
        # 'have h :=').  We search forward from the end of the declaration
        # keyword match to find the first ':=' that closes the signature.
        search_start = decl_match.start()
        assign_match = re.search(r':=', text[search_start:])
        if assign_match is None:
            # No ':=' found -- return the text as-is (could be just a body).
            return text.strip() or None
        assign_pos = search_start + assign_match.start()
        body = text[assign_pos + 2:].strip()
        # body should start with 'by' -- don't strip it
        return body if body else None

    # No declaration found -- treat the whole text as a body.
    return text.strip() or None
