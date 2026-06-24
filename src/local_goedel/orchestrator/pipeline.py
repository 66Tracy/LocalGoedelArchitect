"""Main pipeline orchestrating generate -> prove -> refine loop."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from local_goedel.agents.blueprint_generator import BlueprintGenerator
from local_goedel.agents.blueprint_refiner import BlueprintRefiner
from local_goedel.agents.prover import Prover
from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.lean_assembler import build_final_file, parse_theorem_file
from local_goedel.clients.lean_client import LeanClient
from local_goedel.clients.llm_client import LLMClient
from local_goedel.clients.mathlib_client import MathlibSearchClient
from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import NodeStatus
from local_goedel.logging_utils import get_logger
from local_goedel.orchestrator.artifacts import ArtifactWriter, make_run_id
from local_goedel.orchestrator.scheduler import prove_wave
from local_goedel.orchestrator.state import IterationRecord, RunOutcome
from local_goedel.tools.base import ToolRegistry
from local_goedel.tools.lean_compile import LeanCompileTool
from local_goedel.tools.mathlib_search import MathlibSearchTool

if TYPE_CHECKING:
    from local_goedel.config import Settings


class Pipeline:
    """Full generate -> prove -> refine pipeline."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._logger = get_logger(__name__)

        # Initialize clients
        self._lean_client = LeanClient(settings)
        self._mathlib_client = MathlibSearchClient(settings)
        self._llm_client = LLMClient(settings)

        # Initialize tools + prover
        self._registry = ToolRegistry()
        self._registry.register(LeanCompileTool())
        self._registry.register(MathlibSearchTool())
        self._prover = Prover(self._llm_client, self._registry, settings)

        # Generator + refiner
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
        theorem_file_path: Path | str,
        difficulty: str = "easy",
        max_iter: Optional[int] = None,
    ) -> RunOutcome:
        """Run the full pipeline.

        Args:
            theorem_file_path: Path to the .lean theorem file
            difficulty: "easy" or "hard"
            max_iter: Override max iterations (None = use settings)

        Returns:
            RunOutcome
        """
        theorem_file_path = Path(theorem_file_path)
        theorem_src = theorem_file_path.read_text(encoding="utf-8")
        _, lean_name, target_signature, _ = parse_theorem_file(theorem_src)
        problem_name = theorem_file_path.stem

        # Create run artifacts
        runs_dir = Path(self._settings.runs_dir)
        run_id = make_run_id(problem_name, runs_dir)
        artifacts = ArtifactWriter(run_id, runs_dir, self._logger)

        # Set up run logging to file
        run_log_handler = logging.FileHandler(str(artifacts.log_path()), encoding="utf-8")
        run_log_handler.setLevel(logging.DEBUG)
        run_log_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logging.getLogger("local_goedel").addHandler(run_log_handler)

        self._logger.info("Pipeline.run: %s (difficulty=%s)", problem_name, difficulty)

        # Save config and theorem
        artifacts.save_config(self._settings)
        artifacts.save_theorem(theorem_src)

        start_time = time.time()

        # Determine max iterations
        if max_iter is not None:
            max_iterations = max_iter
        elif difficulty == "hard":
            max_iterations = self._settings.iters_hard
        else:
            max_iterations = self._settings.iters_easy

        # ── Step 1: Generate blueprint ────────────────────────────────────────
        self._logger.info("Generating blueprint...")
        try:
            blueprint = self._generator.generate(theorem_src, difficulty)
        except Exception as e:
            self._logger.error("Blueprint generation failed: %s", e)
            outcome = RunOutcome(
                success=False,
                reason=f"Blueprint generation failed: {e}",
                iterations_used=0,
            )
            artifacts.save_outcome(outcome)
            logging.getLogger("local_goedel").removeHandler(run_log_handler)
            return outcome

        # Auto-satisfy all DEFINITION nodes — they carry real Lean bodies and
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

        # ── Step 2: Prove loop ────────────────────────────────────────────────
        for iteration in range(1, max_iterations + 1):
            # Wall-clock check
            elapsed = time.time() - start_time
            if elapsed > self._settings.max_wall_s:
                self._logger.info("Wall clock exceeded: %.0fs > %ds", elapsed, self._settings.max_wall_s)
                outcome = RunOutcome(
                    success=False,
                    blueprint=blueprint,
                    reason=f"Wall clock exceeded ({elapsed:.0f}s > {self._settings.max_wall_s}s)",
                    iterations_used=iteration - 1,
                )
                artifacts.save_outcome(outcome)
                logging.getLogger("local_goedel").removeHandler(run_log_handler)
                return outcome

            if blueprint.is_solved():
                break

            self._logger.info("=== Iteration %d/%d ===", iteration, max_iterations)

            # Inner wave loop: keep proving as long as something gets proved
            wave_iteration_proved = 0
            wave_num = 0
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
                results=results if 'results' in dir() else [],
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
            old_proved = sum(1 for n in blueprint.nodes.values() if n.status == NodeStatus.PROVED)

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
            new_proved = sum(1 for n in new_blueprint.nodes.values() if n.status == NodeStatus.PROVED)
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
                outcome = RunOutcome(
                    success=False,
                    blueprint=blueprint,
                    reason="Stuck: no progress over 2+ iterations and refiner made no changes",
                    iterations_used=iteration,
                )
                artifacts.save_outcome(outcome)
                logging.getLogger("local_goedel").removeHandler(run_log_handler)
                return outcome

        # ── Step 3: Final gate ────────────────────────────────────────────────
        self._logger.info("Running final gate...")
        outcome = self._finalize(
            blueprint=blueprint,
            lean_name=lean_name,
            artifacts=artifacts,
            iterations_used=len(iteration_records),
        )
        artifacts.save_outcome(outcome)
        logging.getLogger("local_goedel").removeHandler(run_log_handler)
        return outcome

    def _finalize(
        self,
        blueprint: Blueprint,
        lean_name: str,
        artifacts: ArtifactWriter,
        iterations_used: int,
    ) -> RunOutcome:
        """Build final file and verify."""
        reachable = blueprint.reachable_from_target()
        try:
            ordered_ids = [
                nid for nid in blueprint.topo_sort() if nid in reachable
            ]
        except ValueError:
            ordered_ids = [
                nid for nid in blueprint.nodes if nid in reachable
            ]

        ordered_nodes = [blueprint.nodes[nid] for nid in ordered_ids]

        final_code = build_final_file(ordered_nodes, lean_name)
        self._logger.info("Final file: %d nodes, %d lines", len(ordered_nodes), final_code.count("\n"))

        try:
            check = self._lean_client.check(final_code)
        except Exception as e:
            self._logger.error("Final Lean check failed: %s", e)
            artifacts.save_final(final_code)
            return RunOutcome(
                success=False,
                final_code=final_code,
                blueprint=blueprint,
                reason=f"Final Lean check exception: {e}",
                iterations_used=iterations_used,
            )

        artifacts.save_final(final_code, check)

        has_errors = check.has_errors
        sorry_used = uses_sorry(check, lean_name)

        if not has_errors and not sorry_used:
            self._logger.info("FINAL GATE: SUCCESS")
            return RunOutcome(
                success=True,
                final_code=final_code,
                check=check,
                blueprint=blueprint,
                reason="Proved and verified sorry-free",
                iterations_used=iterations_used,
            )
        else:
            reason_parts = []
            if has_errors:
                reason_parts.append(f"{len(check.errors)} errors in final check")
            if sorry_used:
                reason_parts.append("sorry used")
            reason = "; ".join(reason_parts)
            self._logger.info("FINAL GATE: FAILED (%s)", reason)
            return RunOutcome(
                success=False,
                final_code=final_code,
                check=check,
                blueprint=blueprint,
                reason=reason,
                iterations_used=iterations_used,
            )
