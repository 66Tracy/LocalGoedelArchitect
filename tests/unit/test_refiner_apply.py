"""Tests for blueprint refiner ops and proof-reuse."""
from __future__ import annotations

import pytest

from local_goedel.agents.blueprint_refiner import _apply_patch_ops, _apply_proof_reuse
from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus
from local_goedel.domain.results import Diagnosis, DiagnosisKind, ProofResult


def _make_node(
    nid: str,
    parents: list[str] | None = None,
    status: NodeStatus = NodeStatus.PENDING,
    kind: NodeKind = NodeKind.LEMMA,
    lean_name: str | None = None,
    signature: str = "(n : Nat) : True",
    proof: str | None = None,
) -> BlueprintNode:
    node = BlueprintNode(
        id=nid,
        kind=kind,
        lean_name=lean_name or nid,
        signature=signature,
        parents=parents or [],
        status=status,
        proof=proof,
    )
    return node


def _simple_bp() -> Blueprint:
    return Blueprint(
        nodes={
            "lemma_a": _make_node("lemma_a", parents=[], status=NodeStatus.PROVED, proof="by trivial"),
            "target": _make_node("target", parents=["lemma_a"], kind=NodeKind.TARGET,
                                 lean_name="main_theorem", signature="(x : ℕ) : x = x"),
        },
        target_id="target",
    )


# ── add_node ─────────────────────────────────────────────────────────────────

def test_add_node():
    bp = _simple_bp()
    ops = [{
        "op": "add_node",
        "node": {
            "id": "new_lemma",
            "kind": "lemma",
            "lean_name": "new_lemma",
            "signature": "(n : ℕ) : n = n",
            "nl_statement": "",
            "proof_sketch": "",
            "parents": ["lemma_a"],
        },
    }]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    assert "new_lemma" in new_bp.nodes
    assert new_bp.nodes["new_lemma"].parents == ["lemma_a"]


# ── replace_statement ────────────────────────────────────────────────────────

def test_replace_statement():
    bp = _simple_bp()
    ops = [{
        "op": "replace_statement",
        "id": "lemma_a",
        "signature": "(n : ℕ) : n + 0 = n",
        "nl_statement": "new nl",
        "proof_sketch": "new sketch",
    }]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    assert new_bp.nodes["lemma_a"].signature == "(n : ℕ) : n + 0 = n"
    assert new_bp.nodes["lemma_a"].status == NodeStatus.PENDING  # reset


def test_replace_statement_ignores_target():
    """replace_statement on target_id is ignored."""
    bp = _simple_bp()
    ops = [{
        "op": "replace_statement",
        "id": "target",
        "signature": "DIFFERENT_SIG",
    }]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    # target signature must remain canonical
    assert new_bp.nodes["target"].signature == "(x : ℕ) : x = x"


# ── rewire ───────────────────────────────────────────────────────────────────

def test_rewire():
    bp = _simple_bp()
    # Add an extra node first
    bp.nodes["extra"] = _make_node("extra", parents=[])
    ops = [{"op": "rewire", "id": "target", "parents": ["lemma_a", "extra"]}]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    assert set(new_bp.nodes["target"].parents) == {"lemma_a", "extra"}


# ── drop ─────────────────────────────────────────────────────────────────────

def test_drop_node():
    bp = _simple_bp()
    bp.nodes["to_drop"] = _make_node("to_drop", parents=[])
    bp.nodes["target"].parents.append("to_drop")
    ops = [{"op": "drop", "id": "to_drop"}]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    assert "to_drop" not in new_bp.nodes
    # Also removed from target's parents
    assert "to_drop" not in new_bp.nodes["target"].parents


def test_drop_target_is_ignored():
    bp = _simple_bp()
    ops = [{"op": "drop", "id": "target"}]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    assert "target" in new_bp.nodes


# ── decompose ────────────────────────────────────────────────────────────────

def test_decompose():
    bp = _simple_bp()
    ops = [{
        "op": "decompose",
        "id": "target",
        "helpers": [{
            "id": "helper_1",
            "kind": "lemma",
            "lean_name": "helper_1",
            "signature": "(n : ℕ) : n = n",
            "nl_statement": "",
            "proof_sketch": "",
            "parents": ["lemma_a"],
        }],
        "new_parents": ["helper_1"],
    }]
    new_bp = _apply_patch_ops(bp, ops, "main_theorem", "(x : ℕ) : x = x")
    assert "helper_1" in new_bp.nodes
    assert "helper_1" in new_bp.nodes["target"].parents


# ── proof reuse ───────────────────────────────────────────────────────────────

def test_proof_reuse_copies_proved():
    """Byte-identical (lean_name, signature) nodes get PROVED status reused."""
    old_bp = _simple_bp()
    # lemma_a is PROVED in old_bp with proof "by trivial"

    # New blueprint: same lemma_a lean_name+sig but status PENDING
    new_bp = Blueprint(
        nodes={
            "lemma_a": _make_node(
                "lemma_a",
                lean_name="lemma_a",
                signature="(n : Nat) : True",
                status=NodeStatus.PENDING,
            ),
            "target": _make_node(
                "target",
                parents=["lemma_a"],
                kind=NodeKind.TARGET,
                lean_name="main_theorem",
                signature="(x : ℕ) : x = x",
            ),
        },
        target_id="target",
    )

    _apply_proof_reuse(new_bp, old_bp)

    assert new_bp.nodes["lemma_a"].status == NodeStatus.PROVED
    assert new_bp.nodes["lemma_a"].proof == "by trivial"


def test_proof_reuse_does_not_copy_when_sig_differs():
    """Different signature -> no reuse."""
    old_bp = _simple_bp()
    # Change signature in new_bp
    new_bp = Blueprint(
        nodes={
            "lemma_a": _make_node(
                "lemma_a",
                lean_name="lemma_a",
                signature="DIFFERENT_SIGNATURE",
                status=NodeStatus.PENDING,
            ),
            "target": _make_node(
                "target",
                parents=["lemma_a"],
                kind=NodeKind.TARGET,
                lean_name="main_theorem",
                signature="(x : ℕ) : x = x",
            ),
        },
        target_id="target",
    )

    _apply_proof_reuse(new_bp, old_bp)

    # Should remain PENDING
    assert new_bp.nodes["lemma_a"].status == NodeStatus.PENDING
