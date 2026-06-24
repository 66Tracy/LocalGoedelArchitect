"""E2E test: prove the algebra_sqineq theorem."""
from pathlib import Path

import pytest

from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.lean_assembler import build_node_file, parse_theorem_file
from local_goedel.clients.lean_client import LeanClient
from local_goedel.clients.llm_client import LLMClient
from local_goedel.clients.mathlib_client import MathlibSearchClient
from local_goedel.config import load_settings
from local_goedel.domain.node import BlueprintNode, NodeKind
from local_goedel.agents.prover import Prover
from local_goedel.tools.base import ToolRegistry
from local_goedel.tools.lean_compile import LeanCompileTool
from local_goedel.tools.mathlib_search import MathlibSearchTool

THEOREM_FILE = (
    Path(__file__).parents[2]
    / "lean4_problems"
    / "Minif2f"
    / "algebra_sqineq_2atp2bpge2ab.lean"
)

ENV_FILE = Path(__file__).parents[2] / ".env"


@pytest.fixture
def settings():
    return load_settings(env_path=ENV_FILE)


@pytest.fixture
def lean_client(settings):
    return LeanClient(settings)


@pytest.fixture(autouse=True)
def skip_if_no_lean(lean_client):
    if not lean_client.health():
        pytest.skip("Lean server not running")


@pytest.mark.slow
def test_prove_algebra_sqineq(settings, lean_client):
    """Prove a^2 + b^2 >= 2ab using the full prover stack."""
    # Parse the theorem file
    theorem_text = THEOREM_FILE.read_text(encoding="utf-8")
    preamble, lean_name, signature, docstring = parse_theorem_file(theorem_text)

    # Build target node
    target_node = BlueprintNode(
        id="algebra_sqineq",
        lean_name=lean_name,
        signature=signature,
        nl_statement=docstring or "For any two real numbers a and b, a^2 + b^2 >= 2ab.",
        proof_sketch="Follows from (a-b)^2 >= 0 or nlinarith/linarith.",
        kind=NodeKind.TARGET,
    )

    # Set up clients
    mathlib_client = MathlibSearchClient(settings)
    llm_client = LLMClient(settings)

    # Set up registry with tool context
    registry = ToolRegistry()

    # We need to wire clients into tools via context
    # The tools access ctx.lean_client and ctx.mathlib_client
    registry.register(LeanCompileTool())
    registry.register(MathlibSearchTool())

    # Build prover
    prover = Prover(llm=llm_client, registry=registry, settings=settings)

    # Run prove with explicit clients
    result = prover.prove_with_clients(
        target_node=target_node,
        parent_nodes=[],
        lean_client=lean_client,
        mathlib_client=mathlib_client,
        iteration=0,
    )

    print(f"\nProof result: success={result.success}")
    print(f"Proof body: {result.proof_body}")
    if result.check_result:
        print(f"Errors: {[e.data for e in result.check_result.errors]}")

    assert result.success, (
        f"Prover failed. diagnosis={result.diagnosis}, "
        f"proof_body={result.proof_body}"
    )

    # Final sanity check: verify the check is error-free and sorry-free
    assert result.check_result is not None
    assert not result.check_result.has_errors, "Final check has errors"
    assert not uses_sorry(
        result.check_result, lean_name
    ), "Final check uses sorry"
