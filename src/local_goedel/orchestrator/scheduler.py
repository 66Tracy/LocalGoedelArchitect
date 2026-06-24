"""Serial wave scheduler."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import NodeStatus
from local_goedel.domain.results import ProofResult
from local_goedel.logging_utils import get_logger

if TYPE_CHECKING:
    from local_goedel.agents.prover import Prover
    from local_goedel.clients.lean_client import LeanClient
    from local_goedel.clients.mathlib_client import MathlibSearchClient


def prove_wave(
    blueprint: Blueprint,
    prover: "Prover",
    lean_client: "LeanClient",
    mathlib_client: "MathlibSearchClient",
    iteration: int,
    logger: logging.Logger | None = None,
) -> list[ProofResult]:
    """Prove all currently ready nodes (serial).

    For each id in blueprint.ready_nodes(), gather its declared parent BlueprintNodes
    (as sorry stubs for context), call prover.prove_with_clients, and update the
    node's status/proof/proof_result/iteration_proved/attempts.

    Returns list of ProofResults.
    """
    if logger is None:
        logger = get_logger(__name__)

    ready_ids = blueprint.ready_nodes()
    if not ready_ids:
        return []

    logger.info("prove_wave: %d ready nodes: %s", len(ready_ids), ready_ids)
    results: list[ProofResult] = []

    for node_id in ready_ids:
        node = blueprint.nodes[node_id]
        # Gather all declared parents (as sorry stubs)
        parent_nodes = [
            blueprint.nodes[pid]
            for pid in node.parents
            if pid in blueprint.nodes
        ]

        logger.info(
            "Proving node %s (%s) with %d parents",
            node_id, node.lean_name, len(parent_nodes),
        )
        node.status = NodeStatus.PROVING
        node.attempts += 1

        try:
            result = prover.prove_with_clients(
                target_node=node,
                parent_nodes=parent_nodes,
                lean_client=lean_client,
                mathlib_client=mathlib_client,
                iteration=iteration,
            )
        except Exception as e:
            logger.error("Prover raised exception for %s: %s", node_id, e)
            from local_goedel.domain.results import Diagnosis, DiagnosisKind
            result = ProofResult(
                success=False,
                proof_body=None,
                check_result=None,
                diagnosis=Diagnosis(
                    kind=DiagnosisKind.UNKNOWN,
                    message=f"Exception: {e}",
                ),
                iterations=iteration,
            )

        # Attach result to node
        node.proof_result = result

        if result.success:
            node.status = NodeStatus.PROVED
            node.proof = result.proof_body
            node.iteration_proved = iteration
            logger.info("Node %s PROVED", node_id)
        else:
            node.status = NodeStatus.UNPROVED
            logger.info("Node %s UNPROVED", node_id)

        results.append(result)

    return results
