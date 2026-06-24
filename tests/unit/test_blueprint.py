"""Tests for domain/blueprint.py."""
from __future__ import annotations

import pytest

from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus


def _make_node(
    nid: str,
    parents: list[str] | None = None,
    status: NodeStatus = NodeStatus.PENDING,
    kind: NodeKind = NodeKind.LEMMA,
    lean_name: str | None = None,
) -> BlueprintNode:
    return BlueprintNode(
        id=nid,
        kind=kind,
        lean_name=lean_name or nid,
        signature=f"(n : Nat) : True",
        parents=parents or [],
        status=status,
    )


def _simple_blueprint() -> Blueprint:
    """a -> b -> c (target). a has no parents."""
    return Blueprint(
        nodes={
            "a": _make_node("a", parents=[]),
            "b": _make_node("b", parents=["a"]),
            "c": _make_node("c", parents=["b"], kind=NodeKind.TARGET),
        },
        target_id="c",
    )


# ── topo_sort ────────────────────────────────────────────────────────────────

def test_topo_sort_basic():
    bp = _simple_blueprint()
    order = bp.topo_sort()
    assert order.index("a") < order.index("b") < order.index("c")


def test_topo_sort_deterministic():
    """Same graph, same order every call."""
    bp = _simple_blueprint()
    assert bp.topo_sort() == bp.topo_sort()


def test_topo_sort_tie_break():
    """Nodes with same depth sorted alphabetically."""
    bp = Blueprint(
        nodes={
            "root": _make_node("root", parents=[]),
            "z_child": _make_node("z_child", parents=["root"]),
            "a_child": _make_node("a_child", parents=["root"]),
            "target": _make_node("target", parents=["z_child", "a_child"], kind=NodeKind.TARGET),
        },
        target_id="target",
    )
    order = bp.topo_sort()
    # Both children come after root
    assert order.index("root") < order.index("a_child")
    assert order.index("root") < order.index("z_child")
    # a_child before z_child (alphabetical tie-break)
    assert order.index("a_child") < order.index("z_child")


def test_topo_sort_raises_on_cycle():
    """Cycle in graph should raise ValueError."""
    bp = Blueprint(
        nodes={
            "a": _make_node("a", parents=["b"]),
            "b": _make_node("b", parents=["a"]),
        },
        target_id="a",
    )
    with pytest.raises(ValueError, match="cycle"):
        bp.topo_sort()


# ── detect_cycle ─────────────────────────────────────────────────────────────

def test_detect_cycle_no_cycle():
    bp = _simple_blueprint()
    assert bp.detect_cycle() is None


def test_detect_cycle_finds_cycle():
    """Injected cycle is detected."""
    bp = Blueprint(
        nodes={
            "a": _make_node("a", parents=["c"]),
            "b": _make_node("b", parents=["a"]),
            "c": _make_node("c", parents=["b"], kind=NodeKind.TARGET),
        },
        target_id="c",
    )
    cycle = bp.detect_cycle()
    assert cycle is not None
    assert len(cycle) >= 2


# ── reachable_from_target ────────────────────────────────────────────────────

def test_reachable_from_target_all():
    bp = _simple_blueprint()
    reachable = bp.reachable_from_target()
    assert reachable == {"a", "b", "c"}


def test_reachable_from_target_excludes_orphans():
    bp = _simple_blueprint()
    bp.nodes["orphan"] = _make_node("orphan", parents=[])
    reachable = bp.reachable_from_target()
    assert "orphan" not in reachable
    assert reachable == {"a", "b", "c"}


# ── prune_unreachable ─────────────────────────────────────────────────────────

def test_prune_unreachable():
    bp = _simple_blueprint()
    bp.nodes["orphan"] = _make_node("orphan", parents=[])
    removed = bp.prune_unreachable()
    assert "orphan" in removed
    assert "orphan" not in bp.nodes
    assert "a" in bp.nodes


# ── ready_nodes ──────────────────────────────────────────────────────────────

def test_ready_nodes_empty_when_no_parents_proved():
    """b is pending, parent a is pending -> b not ready."""
    bp = _simple_blueprint()
    # a is pending (not proved), so b (and c) should not be ready
    # But a has no parents -> a IS ready (trivially, all 0 parents proved)
    ready = bp.ready_nodes()
    assert "a" in ready
    assert "b" not in ready
    assert "c" not in ready


def test_ready_nodes_after_parent_proved():
    """After a is PROVED, b becomes ready."""
    bp = _simple_blueprint()
    bp.nodes["a"].status = NodeStatus.PROVED
    ready = bp.ready_nodes()
    assert "b" in ready
    assert "c" not in ready


def test_ready_nodes_in_topo_order():
    """ready_nodes returns nodes in topological order."""
    bp = _simple_blueprint()
    bp.nodes["a"].status = NodeStatus.PROVED
    bp.nodes["b"].status = NodeStatus.PROVED
    ready = bp.ready_nodes()
    assert ready == ["c"]


def test_ready_nodes_includes_unproved():
    """UNPROVED nodes with all parents proved are also ready."""
    bp = _simple_blueprint()
    bp.nodes["a"].status = NodeStatus.PROVED
    bp.nodes["b"].status = NodeStatus.UNPROVED  # failed before, retry
    ready = bp.ready_nodes()
    assert "b" in ready


# ── is_solved ─────────────────────────────────────────────────────────────────

def test_is_solved_false_initially():
    bp = _simple_blueprint()
    assert not bp.is_solved()


def test_is_solved_true_when_target_proved():
    bp = _simple_blueprint()
    bp.nodes["c"].status = NodeStatus.PROVED
    assert bp.is_solved()


# ── validate ─────────────────────────────────────────────────────────────────

def test_validate_clean_blueprint():
    bp = _simple_blueprint()
    errors = bp.validate()
    assert errors == []


def test_validate_no_target():
    bp = Blueprint(
        nodes={"a": _make_node("a"), "b": _make_node("b", parents=["a"])},
        target_id="b",
    )
    errors = bp.validate()
    assert any("TARGET" in e for e in errors)


def test_validate_multiple_targets():
    bp = Blueprint(
        nodes={
            "a": _make_node("a", kind=NodeKind.TARGET),
            "b": _make_node("b", kind=NodeKind.TARGET),
        },
        target_id="a",
    )
    errors = bp.validate()
    assert any("Multiple TARGET" in e for e in errors)


def test_validate_missing_parent():
    bp = Blueprint(
        nodes={"a": _make_node("a", parents=["nonexistent"])},
        target_id="a",
    )
    bp.nodes["a"].kind = NodeKind.TARGET
    errors = bp.validate()
    assert any("nonexistent" in e for e in errors)


def test_validate_duplicate_lean_name():
    bp = Blueprint(
        nodes={
            "a": _make_node("a", lean_name="same_name"),
            "b": _make_node("b", parents=["a"], lean_name="same_name", kind=NodeKind.TARGET),
        },
        target_id="b",
    )
    errors = bp.validate()
    assert any("same_name" in e and "Duplicate" in e for e in errors)


def test_validate_cycle_detected():
    bp = Blueprint(
        nodes={
            "a": _make_node("a", parents=["b"]),
            "b": _make_node("b", parents=["a"], kind=NodeKind.TARGET),
        },
        target_id="b",
    )
    errors = bp.validate()
    assert any("Cycle" in e or "cycle" in e for e in errors)


# ── JSON round-trip ───────────────────────────────────────────────────────────

def test_json_roundtrip():
    bp = _simple_blueprint()
    bp.nodes["a"].status = NodeStatus.PROVED
    bp.nodes["a"].proof = "by trivial"

    data = bp.to_json()
    bp2 = Blueprint.from_json(data)

    assert set(bp2.nodes.keys()) == {"a", "b", "c"}
    assert bp2.target_id == "c"
    assert bp2.nodes["a"].status == NodeStatus.PROVED
    assert bp2.nodes["a"].proof == "by trivial"
    assert bp2.nodes["b"].parents == ["a"]
