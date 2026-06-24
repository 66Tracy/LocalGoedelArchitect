"""Parsers for LLM outputs (blueprint JSON, patch ops, etc.)."""
from __future__ import annotations

import json
import re
from typing import Optional

from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus


def _extract_json_object(text: str) -> Optional[str]:
    """Extract the first JSON object from text, even if wrapped in prose/markdown."""
    # Try fenced code block first: ```json ... ``` or ``` ... ```
    fence_match = re.search(
        r'```(?:json)?\s*\n?([\s\S]*?)\n?```',
        text,
        re.DOTALL,
    )
    if fence_match:
        candidate = fence_match.group(1).strip()
        # Check it's a JSON object
        if candidate.startswith('{'):
            return candidate

    # Try to find raw JSON object: find first '{' and match braces
    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _coerce_kind(raw: str) -> NodeKind:
    """Coerce a string to NodeKind."""
    raw = raw.lower().strip()
    if raw in ("definition", "def"):
        return NodeKind.DEFINITION
    if raw in ("lemma", "helper", "auxiliary"):
        return NodeKind.LEMMA
    if raw in ("theorem", "target", "main"):
        return NodeKind.TARGET
    # Default to LEMMA
    return NodeKind.LEMMA


def parse_blueprint_json(
    text: str,
    target_lean_name: str,
    target_signature: str,
) -> Blueprint:
    """Parse a blueprint JSON from LLM output (tolerant of markdown fences/prose).

    Args:
        text: Raw LLM output (may contain prose + JSON)
        target_lean_name: The canonical target theorem lean name (forced onto target node)
        target_signature: The canonical target theorem signature (forced onto target node)

    Returns:
        Blueprint with all nodes PENDING and target node correctly set

    Raises:
        ValueError: With descriptive error listing problems
    """
    json_str = _extract_json_object(text)
    if json_str is None:
        raise ValueError(
            f"No JSON object found in LLM output. "
            f"Expected a JSON object with 'nodes' array and 'target_id'. "
            f"Output was:\n{text[:500]}"
        )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Invalid JSON in LLM output: {e}\n"
            f"JSON snippet:\n{json_str[:500]}"
        ) from e

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at top level, got {type(data).__name__}")

    nodes_raw = data.get("nodes", [])
    if not isinstance(nodes_raw, list):
        raise ValueError(f"'nodes' must be a list, got {type(nodes_raw).__name__}")

    target_id_raw = data.get("target_id", "")

    errors: list[str] = []
    nodes: dict[str, BlueprintNode] = {}

    for i, raw_node in enumerate(nodes_raw):
        if not isinstance(raw_node, dict):
            errors.append(f"Node {i}: expected dict, got {type(raw_node).__name__}")
            continue

        node_id = raw_node.get("id", "").strip()
        if not node_id:
            node_id = raw_node.get("lean_name", f"node_{i}").strip()
            if not node_id:
                errors.append(f"Node {i}: missing 'id' field")
                continue

        lean_name = raw_node.get("lean_name", "").strip()
        if not lean_name:
            errors.append(f"Node '{node_id}': missing 'lean_name'")
            lean_name = node_id

        signature = raw_node.get("signature", "").strip()
        if not signature:
            errors.append(f"Node '{node_id}': missing 'signature'")

        kind_raw = raw_node.get("kind", "lemma")
        kind = _coerce_kind(str(kind_raw))

        parents_raw = raw_node.get("parents", [])
        if not isinstance(parents_raw, list):
            parents_raw = []
        parents = [str(p).strip() for p in parents_raw if str(p).strip()]

        # For DEFINITION nodes, pick up any verbatim Lean declaration the LLM
        # provided under "definition_text".  If absent, the assembler will fall
        # back to constructing "def <name> <signature>" (which works when the
        # LLM embedded ':= body' in the signature field).
        definition_text: Optional[str] = None
        if kind == NodeKind.DEFINITION:
            raw_def_text = raw_node.get("definition_text", "")
            if raw_def_text and str(raw_def_text).strip():
                definition_text = str(raw_def_text).strip()

        node = BlueprintNode(
            id=node_id,
            kind=kind,
            lean_name=lean_name,
            signature=signature,
            definition_text=definition_text,
            nl_statement=raw_node.get("nl_statement", ""),
            proof_sketch=raw_node.get("proof_sketch", ""),
            parents=parents,
            status=NodeStatus.PENDING,
        )
        nodes[node_id] = node

    if errors and not nodes:
        raise ValueError("Multiple errors parsing blueprint JSON:\n" + "\n".join(errors))

    # Determine target_id: use target_id from JSON, or find single TARGET-kind node,
    # or last node with kind TARGET in list
    target_id = target_id_raw.strip()

    # Find the TARGET node (there should be exactly one)
    target_nodes = [nid for nid, n in nodes.items() if n.kind == NodeKind.TARGET]

    if not target_id:
        if len(target_nodes) == 1:
            target_id = target_nodes[0]
        elif target_nodes:
            target_id = target_nodes[-1]
        elif nodes:
            # Fall back to last node in nodes list
            target_id = list(nodes.keys())[-1]

    # If target_id not in nodes, try to find by lean_name
    if target_id and target_id not in nodes:
        for nid, n in nodes.items():
            if n.lean_name == target_id:
                target_id = nid
                break

    # Force the target node's lean_name and signature to canonical values
    if target_id and target_id in nodes:
        target_node = nodes[target_id]
        target_node.kind = NodeKind.TARGET
        target_node.lean_name = target_lean_name
        target_node.signature = target_signature
    elif nodes:
        # No valid target_id found - use last node
        target_id = list(nodes.keys())[-1]
        nodes[target_id].kind = NodeKind.TARGET
        nodes[target_id].lean_name = target_lean_name
        nodes[target_id].signature = target_signature
        errors.append(f"Could not determine target_id from JSON; using '{target_id}' as fallback")

    # Mark all non-target TARGET-kind nodes as LEMMA
    for nid, node in nodes.items():
        if nid != target_id and node.kind == NodeKind.TARGET:
            node.kind = NodeKind.LEMMA

    blueprint = Blueprint(nodes=nodes, target_id=target_id)

    # Collect validation errors
    validation_errors = blueprint.validate()
    all_errors = errors + validation_errors
    if all_errors:
        # Don't raise if only "dead node" warnings or minor issues
        fatal = [e for e in all_errors if "dead node" not in e and "not reachable" not in e]
        if fatal:
            raise ValueError(
                "Blueprint has errors:\n" + "\n".join(all_errors)
            )

    return blueprint
