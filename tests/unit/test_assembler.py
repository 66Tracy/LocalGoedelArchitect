"""Tests for lean assembler."""
import pytest

from local_goedel.assembly.lean_assembler import (
    build_final_file,
    build_node_file,
    parse_theorem_file,
    render_decl,
)
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus


def _make_node(id_, lean_name, signature, proof=None):
    return BlueprintNode(
        id=id_,
        lean_name=lean_name,
        signature=signature,
        proof=proof,
        kind=NodeKind.TARGET,
    )


def test_render_decl_with_body():
    result = render_decl("myThm", "(a b : Nat) : a + b = b + a", "by ring")
    assert result == "theorem myThm (a b : Nat) : a + b = b + a := by ring"


def test_render_decl_no_body():
    result = render_decl("myThm", "(n : Nat) : n = n", None)
    assert result == "theorem myThm (n : Nat) : n = n := by sorry"


def test_build_node_file_with_parent():
    parent = _make_node("p1", "parentLemma", "(n : Nat) : n >= 0")
    target = _make_node("t1", "mainThm", "(a b : Real) : a + b = b + a")

    result = build_node_file(target, [parent], proof_body="by ring")

    assert "import Mathlib" in result
    assert "theorem parentLemma (n : Nat) : n >= 0 := by sorry" in result
    assert "theorem mainThm (a b : Real) : a + b = b + a := by ring" in result
    assert "#print axioms mainThm" in result


def test_build_node_file_no_parent():
    target = _make_node("t1", "mainThm", "(a b : Real) : a + b = b + a")
    result = build_node_file(target, [], proof_body="by ring")

    assert "import Mathlib" in result
    assert "theorem mainThm (a b : Real) : a + b = b + a := by ring" in result
    assert "#print axioms mainThm" in result
    # No sorry stubs for parents
    lines = result.splitlines()
    sorry_lines = [l for l in lines if "sorry" in l and "theorem" in l]
    assert len(sorry_lines) == 0


def test_build_node_file_import_first():
    target = _make_node("t1", "foo", "(n : Nat) : n = n")
    result = build_node_file(target, [], proof_body="by rfl")
    assert result.startswith("import Mathlib")


def test_parse_theorem_file():
    text = """\
import Mathlib

/-- For any two real numbers a and b, show that a^2 + b^2 >= 2ab. -/
theorem algebra_sqineq (a b : Real) : a ^ 2 + b ^ 2 >= 2 * a * b := by
  sorry
"""
    preamble, lean_name, sig, doc = parse_theorem_file(text)
    assert "import Mathlib" in preamble[0]
    assert lean_name == "algebra_sqineq"
    assert "(a b : Real)" in sig
    assert "a ^ 2 + b ^ 2" in sig
    assert "2ab" in doc or "2 * a * b" in doc or len(doc) > 0
