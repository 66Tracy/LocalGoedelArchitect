"""Tests for agents/parsers.py."""
from __future__ import annotations

import json

import pytest

from local_goedel.agents.parsers import parse_blueprint_json
from local_goedel.domain.node import NodeKind, NodeStatus


_TARGET_NAME = "my_theorem"
_TARGET_SIG = "(n : ℕ) : n + 0 = n"

_MINIMAL_JSON = json.dumps({
    "nodes": [
        {
            "id": "my_theorem",
            "kind": "theorem",
            "lean_name": "my_theorem",
            "signature": "(n : ℕ) : n + 0 = n",
            "nl_statement": "n+0=n",
            "proof_sketch": "Follows from add_zero.",
            "parents": [],
        }
    ],
    "target_id": "my_theorem",
})

_TWO_NODE_JSON = json.dumps({
    "nodes": [
        {
            "id": "helper",
            "kind": "lemma",
            "lean_name": "helper_lemma",
            "signature": "(n : ℕ) : n + 0 = n",
            "nl_statement": "helper",
            "proof_sketch": "add_zero",
            "parents": [],
        },
        {
            "id": "main",
            "kind": "theorem",
            "lean_name": "my_theorem",
            "signature": "(n : ℕ) : n + 0 = n",
            "nl_statement": "main",
            "proof_sketch": "use helper_lemma",
            "parents": ["helper"],
        },
    ],
    "target_id": "main",
})


def test_parse_minimal_json():
    bp = parse_blueprint_json(_MINIMAL_JSON, _TARGET_NAME, _TARGET_SIG)
    assert "my_theorem" in bp.nodes
    assert bp.target_id == "my_theorem"
    assert bp.nodes["my_theorem"].kind == NodeKind.TARGET
    assert bp.nodes["my_theorem"].lean_name == _TARGET_NAME
    assert bp.nodes["my_theorem"].signature == _TARGET_SIG


def test_parse_json_in_markdown_fence():
    """JSON wrapped in ```json ... ``` fences."""
    text = f"Here is the blueprint:\n```json\n{_MINIMAL_JSON}\n```\nThat's it."
    bp = parse_blueprint_json(text, _TARGET_NAME, _TARGET_SIG)
    assert "my_theorem" in bp.nodes
    assert bp.target_id == "my_theorem"


def test_parse_json_in_prose():
    """JSON embedded in plain prose."""
    text = f"I designed the following graph: {_MINIMAL_JSON} Hope that helps."
    bp = parse_blueprint_json(text, _TARGET_NAME, _TARGET_SIG)
    assert "my_theorem" in bp.nodes


def test_parse_two_node_blueprint():
    bp = parse_blueprint_json(_TWO_NODE_JSON, _TARGET_NAME, _TARGET_SIG)
    assert "helper" in bp.nodes
    assert "main" in bp.nodes
    assert bp.target_id == "main"
    assert bp.nodes["main"].kind == NodeKind.TARGET
    # Helper kind is LEMMA
    assert bp.nodes["helper"].kind == NodeKind.LEMMA


def test_canonical_target_forced():
    """Target node's lean_name and signature are overridden to canonical values."""
    json_str = json.dumps({
        "nodes": [
            {
                "id": "tgt",
                "kind": "theorem",
                "lean_name": "wrong_name",
                "signature": "wrong_sig",
                "nl_statement": "",
                "proof_sketch": "",
                "parents": [],
            }
        ],
        "target_id": "tgt",
    })
    bp = parse_blueprint_json(json_str, "correct_name", "correct_sig")
    assert bp.nodes["tgt"].lean_name == "correct_name"
    assert bp.nodes["tgt"].signature == "correct_sig"


def test_all_nodes_pending():
    """Parsed blueprint nodes should all start as PENDING."""
    bp = parse_blueprint_json(_TWO_NODE_JSON, _TARGET_NAME, _TARGET_SIG)
    for node in bp.nodes.values():
        assert node.status == NodeStatus.PENDING


def test_error_on_no_json():
    """Raises ValueError if no JSON found."""
    with pytest.raises(ValueError, match="No JSON"):
        parse_blueprint_json("No JSON here at all!", _TARGET_NAME, _TARGET_SIG)


def test_kind_coercion_def():
    """'definition' kind coerced correctly."""
    json_str = json.dumps({
        "nodes": [
            {
                "id": "defn",
                "kind": "definition",
                "lean_name": "my_def",
                "signature": "(n : ℕ) := n",
                "nl_statement": "",
                "proof_sketch": "",
                "parents": [],
            },
            {
                "id": "tgt",
                "kind": "theorem",
                "lean_name": _TARGET_NAME,
                "signature": _TARGET_SIG,
                "nl_statement": "",
                "proof_sketch": "uses my_def",
                "parents": ["defn"],
            },
        ],
        "target_id": "tgt",
    })
    bp = parse_blueprint_json(json_str, _TARGET_NAME, _TARGET_SIG)
    assert bp.nodes["defn"].kind == NodeKind.DEFINITION
    assert bp.nodes["tgt"].kind == NodeKind.TARGET
