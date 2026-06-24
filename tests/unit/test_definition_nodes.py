"""Tests for DEFINITION node handling in assembler, blueprint, and pipeline."""
from __future__ import annotations

import pytest

from local_goedel.assembly.lean_assembler import (
    build_final_file,
    build_node_file,
    build_skeleton_file,
    render_definition,
)
from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus


# ── helpers ──────────────────────────────────────────────────────────────────

def _def_node(
    nid: str,
    lean_name: str,
    signature: str,
    definition_text: str | None = None,
    parents: list[str] | None = None,
    status: NodeStatus = NodeStatus.PENDING,
) -> BlueprintNode:
    return BlueprintNode(
        id=nid,
        kind=NodeKind.DEFINITION,
        lean_name=lean_name,
        signature=signature,
        definition_text=definition_text,
        parents=parents or [],
        status=status,
    )


def _lemma_node(
    nid: str,
    lean_name: str,
    signature: str,
    parents: list[str] | None = None,
    status: NodeStatus = NodeStatus.PENDING,
    proof: str | None = None,
) -> BlueprintNode:
    return BlueprintNode(
        id=nid,
        kind=NodeKind.LEMMA,
        lean_name=lean_name,
        signature=signature,
        parents=parents or [],
        status=status,
        proof=proof,
    )


def _target_node(
    nid: str,
    lean_name: str,
    signature: str,
    parents: list[str] | None = None,
    status: NodeStatus = NodeStatus.PENDING,
    proof: str | None = None,
) -> BlueprintNode:
    return BlueprintNode(
        id=nid,
        kind=NodeKind.TARGET,
        lean_name=lean_name,
        signature=signature,
        parents=parents or [],
        status=status,
        proof=proof,
    )


def _simple_def_blueprint() -> Blueprint:
    """
    Graph:
      def_abs (DEFINITION) -> lemma_sign (LEMMA) -> target_thm (TARGET)

    The definition carries a real body in definition_text.
    """
    def_node = _def_node(
        "def_abs",
        lean_name="my_abs",
        signature="(x : Int) : Int",
        definition_text="def my_abs (x : Int) : Int := if x >= 0 then x else -x",
    )
    lemma = _lemma_node(
        "lemma_sign",
        lean_name="my_abs_nonneg",
        signature="(x : Int) : my_abs x >= 0",
        parents=["def_abs"],
    )
    target = _target_node(
        "target_thm",
        lean_name="putnam_trivial",
        signature="(n : Int) : my_abs n >= 0",
        parents=["lemma_sign"],
    )
    return Blueprint(
        nodes={
            "def_abs": def_node,
            "lemma_sign": lemma,
            "target_thm": target,
        },
        target_id="target_thm",
    )


# ── render_definition ────────────────────────────────────────────────────────

class TestRenderDefinition:
    def test_uses_definition_text_when_provided(self):
        node = _def_node(
            "d1", "my_abs", "(x : Int) : Int",
            definition_text="def my_abs (x : Int) : Int := if x >= 0 then x else -x",
        )
        result = render_definition(node)
        assert result == "def my_abs (x : Int) : Int := if x >= 0 then x else -x"

    def test_uses_signature_with_body_when_no_definition_text(self):
        node = _def_node(
            "d1", "my_abs",
            signature="(x : Int) : Int := if x >= 0 then x else -x",
        )
        result = render_definition(node)
        assert result.startswith("def my_abs")
        assert ":=" in result
        assert "theorem" not in result

    def test_fallback_sorry_when_no_body(self):
        node = _def_node(
            "d1", "my_fn", "(x : Int) : Int",
        )
        result = render_definition(node)
        assert result.startswith("def my_fn")
        assert "theorem" not in result
        # A sorry placeholder is acceptable fallback
        assert ":=" in result

    def test_never_emits_theorem_keyword(self):
        for sig_variant in [
            "(x : Int) : Int := x + 1",
            "(x : Int) : Int",
        ]:
            node = _def_node("d1", "f", sig_variant)
            result = render_definition(node)
            assert "theorem" not in result, f"'theorem' found in: {result!r}"


# ── build_node_file: definition parent ───────────────────────────────────────

class TestBuildNodeFileWithDefinitionParent:
    def test_definition_parent_renders_as_def_not_theorem(self):
        def_node = _def_node(
            "def_abs", "my_abs", "(x : Int) : Int",
            definition_text="def my_abs (x : Int) : Int := if x >= 0 then x else -x",
            status=NodeStatus.PROVED,
        )
        lemma = _lemma_node(
            "lem1", "my_abs_nonneg", "(x : Int) : my_abs x >= 0",
        )
        result = build_node_file(lemma, [def_node], proof_body="by simp [my_abs]")

        assert "import Mathlib" in result
        # Definition must appear as 'def', NOT as 'theorem'
        assert "def my_abs (x : Int) : Int := if x >= 0 then x else -x" in result
        assert "theorem my_abs " not in result
        # Lemma (the target being proved) is rendered as theorem
        assert "theorem my_abs_nonneg" in result

    def test_definition_parent_without_definition_text_uses_signature(self):
        def_node = _def_node(
            "def_abs", "my_abs",
            signature="(x : Int) : Int := if x >= 0 then x else -x",
            status=NodeStatus.PROVED,
        )
        lemma = _lemma_node("lem1", "check_it", "(n : Int) : True")
        result = build_node_file(lemma, [def_node], proof_body="by trivial")

        assert "def my_abs" in result
        assert "theorem my_abs" not in result

    def test_lemma_parent_still_gets_sorry_stub(self):
        lemma_parent = _lemma_node(
            "lp", "helper_lemma", "(n : Int) : n + 0 = n",
        )
        target = _target_node(
            "tgt", "main_thm", "(n : Int) : n = n", parents=["lp"]
        )
        result = build_node_file(target, [lemma_parent], proof_body="by rfl")

        assert "theorem helper_lemma (n : Int) : n + 0 = n := by sorry" in result


# ── build_skeleton_file ───────────────────────────────────────────────────────

class TestBuildSkeletonFile:
    def test_definition_in_skeleton_is_def_not_theorem(self):
        bp = _simple_def_blueprint()
        skeleton = build_skeleton_file(bp)
        # Definition must be 'def my_abs ...'
        assert "def my_abs" in skeleton
        # The definition itself must NOT be emitted as a theorem declaration.
        # "theorem my_abs_nonneg" is fine (that's the lemma); we check that
        # no line starts with "theorem my_abs " (the definition name).
        skeleton_lines = skeleton.splitlines()
        def_as_theorem = [
            ln for ln in skeleton_lines if ln.startswith("theorem my_abs ")
        ]
        assert def_as_theorem == [], (
            f"Definition emitted as theorem: {def_as_theorem}"
        )
        # Lemma must be sorry stub
        assert "theorem my_abs_nonneg" in skeleton
        assert "by sorry" in skeleton

    def test_skeleton_starts_with_import(self):
        bp = _simple_def_blueprint()
        skeleton = build_skeleton_file(bp)
        assert skeleton.startswith("import Mathlib")


# ── build_final_file ──────────────────────────────────────────────────────────

class TestBuildFinalFile:
    def test_definition_in_final_file_is_def_not_theorem(self):
        def_node = _def_node(
            "def_abs", "my_abs", "(x : Int) : Int",
            definition_text="def my_abs (x : Int) : Int := if x >= 0 then x else -x",
            status=NodeStatus.PROVED,
        )
        lemma = _lemma_node(
            "lem1", "my_abs_nonneg", "(x : Int) : my_abs x >= 0",
            status=NodeStatus.PROVED,
            proof="by simp [my_abs]",
        )
        target = _target_node(
            "tgt", "putnam_trivial", "(n : Int) : my_abs n >= 0",
            status=NodeStatus.PROVED,
            proof="by exact my_abs_nonneg n",
        )
        result = build_final_file([def_node, lemma, target], "putnam_trivial")

        assert "def my_abs (x : Int) : Int := if x >= 0 then x else -x" in result
        assert "theorem my_abs " not in result
        assert "theorem my_abs_nonneg" in result
        assert "theorem putnam_trivial" in result
        assert "#print axioms putnam_trivial" in result


# ── Blueprint.auto_satisfy_definitions ───────────────────────────────────────

class TestAutoSatisfyDefinitions:
    def test_definition_nodes_are_auto_proved(self):
        bp = _simple_def_blueprint()
        assert bp.nodes["def_abs"].status == NodeStatus.PENDING

        satisfied = bp.auto_satisfy_definitions()

        assert "def_abs" in satisfied
        assert bp.nodes["def_abs"].status == NodeStatus.PROVED

    def test_lemma_and_target_are_not_auto_proved(self):
        bp = _simple_def_blueprint()
        bp.auto_satisfy_definitions()

        assert bp.nodes["lemma_sign"].status == NodeStatus.PENDING
        assert bp.nodes["target_thm"].status == NodeStatus.PENDING

    def test_already_proved_definition_not_re_added_to_satisfied(self):
        bp = _simple_def_blueprint()
        bp.nodes["def_abs"].status = NodeStatus.PROVED

        satisfied = bp.auto_satisfy_definitions()

        # Already proved — should not appear in the "newly satisfied" list
        assert "def_abs" not in satisfied

    def test_multiple_definitions_all_satisfied(self):
        def_a = _def_node("da", "fn_a", "(x : Int) : Int", definition_text="def fn_a (x : Int) : Int := x")
        def_b = _def_node("db", "fn_b", "(x : Int) : Int", definition_text="def fn_b (x : Int) : Int := x + 1")
        target = _target_node("t", "main", "(n : Int) : True", parents=["da", "db"])
        bp = Blueprint(nodes={"da": def_a, "db": def_b, "t": target}, target_id="t")

        satisfied = bp.auto_satisfy_definitions()

        assert set(satisfied) == {"da", "db"}
        assert bp.nodes["da"].status == NodeStatus.PROVED
        assert bp.nodes["db"].status == NodeStatus.PROVED


# ── Blueprint.ready_nodes excludes definitions ───────────────────────────────

class TestReadyNodesExcludesDefinitions:
    def test_definition_not_in_ready_nodes(self):
        bp = _simple_def_blueprint()
        # Before auto-satisfy: definition is PENDING but should not appear
        ready = bp.ready_nodes()
        assert "def_abs" not in ready

    def test_definition_auto_satisfied_enables_dependents(self):
        bp = _simple_def_blueprint()

        # Before auto-satisfy: lemma_sign depends on def_abs (PENDING)
        # def_abs won't appear; lemma_sign parent not PROVED -> not ready
        ready_before = bp.ready_nodes()
        assert "lemma_sign" not in ready_before
        assert "def_abs" not in ready_before

        # After auto-satisfy: def_abs is PROVED -> lemma_sign is ready
        bp.auto_satisfy_definitions()
        ready_after = bp.ready_nodes()
        assert "lemma_sign" in ready_after
        assert "def_abs" not in ready_after

    def test_definition_never_in_ready_nodes_even_when_pending(self):
        """Even with no parents, a DEFINITION node must not enter ready_nodes."""
        def_node = _def_node("d", "my_fn", "(x : Int) : Int", definition_text="def my_fn (x : Int) : Int := x")
        target = _target_node("t", "main", "(n : Int) : True", parents=["d"])
        bp = Blueprint(nodes={"d": def_node, "t": target}, target_id="t")

        ready = bp.ready_nodes()
        assert "d" not in ready

    def test_target_ready_after_all_proved(self):
        bp = _simple_def_blueprint()
        bp.auto_satisfy_definitions()
        bp.nodes["lemma_sign"].status = NodeStatus.PROVED

        ready = bp.ready_nodes()
        assert "target_thm" in ready
        assert "def_abs" not in ready

    def test_is_solved_not_affected_by_definition_nodes(self):
        """is_solved() must key only on the TARGET node."""
        bp = _simple_def_blueprint()
        bp.auto_satisfy_definitions()
        assert not bp.is_solved()

        bp.nodes["lemma_sign"].status = NodeStatus.PROVED
        bp.nodes["target_thm"].status = NodeStatus.PROVED
        assert bp.is_solved()


# ── JSON round-trip preserves definition_text ─────────────────────────────────

class TestDefinitionTextJsonRoundtrip:
    def test_definition_text_survives_json_roundtrip(self):
        bp = _simple_def_blueprint()
        data = bp.to_json()
        bp2 = Blueprint.from_json(data)

        assert bp2.nodes["def_abs"].definition_text == (
            "def my_abs (x : Int) : Int := if x >= 0 then x else -x"
        )
        assert bp2.nodes["def_abs"].kind == NodeKind.DEFINITION

    def test_none_definition_text_survives_json_roundtrip(self):
        bp = _simple_def_blueprint()
        bp.nodes["def_abs"].definition_text = None
        data = bp.to_json()
        bp2 = Blueprint.from_json(data)
        assert bp2.nodes["def_abs"].definition_text is None
