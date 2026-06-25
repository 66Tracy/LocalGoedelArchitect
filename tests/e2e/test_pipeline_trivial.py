"""E2E test: run the full pipeline on the trivial algebra_sqineq theorem."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _safe_print(*args, **kwargs):
    """Print with ASCII-safe encoding for Windows GBK terminals."""
    text = " ".join(str(a) for a in args)
    safe = text.encode("ascii", errors="backslashreplace").decode("ascii")
    print(safe, **kwargs)

from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.assembly.lean_assembler import parse_theorem_file
from local_goedel.clients.lean_client import LeanClient
from local_goedel.config import load_settings
from local_goedel.orchestrator.pipeline import Pipeline

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


@pytest.fixture(autouse=True)
def skip_if_no_api_key(settings):
    if not settings.api_key:
        pytest.skip("No API key found")


@pytest.mark.slow
def test_full_pipeline_algebra_sqineq(settings):
    """Run the full generate->prove->finalize pipeline on a trivial theorem."""
    theorem_text = THEOREM_FILE.read_text(encoding="utf-8")
    _, lean_name, signature, _ = parse_theorem_file(theorem_text)

    _safe_print(f"\nTheorem: {lean_name}")
    _safe_print(f"Signature: {signature}")

    pipeline = Pipeline(settings)
    outcome = pipeline.run(
        theorem_file_path=THEOREM_FILE,
        max_iter=2,
    )

    _safe_print(f"Success: {outcome.success}")
    _safe_print(f"Reason: {outcome.reason}")
    _safe_print(f"Iterations used: {outcome.iterations_used}")
    if outcome.final_code:
        _safe_print(f"Final code length: {len(outcome.final_code)} chars")
    if outcome.check:
        _safe_print(f"Final check errors: {[e.data for e in outcome.check.errors]}")
        _safe_print(f"Uses sorry: {uses_sorry(outcome.check, lean_name)}")

    # The trivial theorem should be solvable
    assert outcome.success, (
        f"Pipeline failed: {outcome.reason}\n"
        f"Blueprint nodes: {len(outcome.blueprint.nodes) if outcome.blueprint else 'N/A'}"
    )
    assert outcome.final_code is not None
    assert outcome.check is not None
    assert not outcome.check.has_errors, (
        "Final check has errors: " +
        str([e.data.encode("ascii", errors="backslashreplace").decode() for e in outcome.check.errors])
    )
    assert not uses_sorry(outcome.check, lean_name), "Final proof uses sorry!"
