"""Blueprint refiner agent."""
from __future__ import annotations

import copy
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from local_goedel.agents.parsers import parse_blueprint_json
from local_goedel.agents.prompts import REFINER_SYSTEM_PROMPT
from local_goedel.assembly.lean_assembler import build_skeleton_file
from local_goedel.clients.lean_client import LeanServerError
from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus
from local_goedel.domain.results import DiagnosisKind
from local_goedel.logging_utils import get_logger

if TYPE_CHECKING:
    from local_goedel.clients.lean_client import LeanClient
    from local_goedel.clients.llm_client import LLMClient
    from local_goedel.config import Settings


def _extract_json_object(text: str) -> Optional[str]:
    """Extract the first JSON object from text."""
    import re
    fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate.startswith('{'):
            return candidate
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


def _render_blueprint_for_refiner(blueprint: Blueprint) -> str:
    """Render the blueprint state as text for the refiner prompt."""
    try:
        order = blueprint.topo_sort()
    except ValueError:
        order = list(blueprint.nodes.keys())

    lines = []
    for nid in order:
        node = blueprint.nodes[nid]
        status_marker = "PROVED" if node.status == NodeStatus.PROVED else "UNPROVED"
        lines.append(f"-- Node: {nid} [{node.kind.value}] -- {status_marker}")
        lines.append(f"-- lean_name: {node.lean_name}")
        lines.append(f"-- signature: {node.signature}")
        if node.nl_statement:
            lines.append(f"-- nl: {node.nl_statement}")
        if node.parents:
            lines.append(f"-- parents: {node.parents}")

        if node.status != NodeStatus.PROVED and node.proof_result is not None:
            pr = node.proof_result
            # Get diagnosis from proof_result
            diag = getattr(pr, "diagnosis", None)
            if diag is not None:
                lines.append(f"## Diagnosis: {diag.kind.value}")
                if diag.analysis:
                    lines.append(f"## Analysis: {diag.analysis[:400]}")
                if diag.suggested_fix:
                    lines.append(f"## Suggested Fix: {diag.suggested_fix[:300]}")
                if diag.suggested_helpers:
                    lines.append("## Suggested Helpers:")
                    for h in diag.suggested_helpers:
                        lines.append(f"  - {h.lean_name}: {h.signature}")
        lines.append("")

    return "\n".join(lines)


def _apply_patch_ops(
    blueprint: Blueprint,
    ops: list[dict[str, Any]],
    target_lean_name: str,
    target_signature: str,
) -> Blueprint:
    """Apply patch operations to a blueprint copy."""
    # Work on a deep copy
    new_bp = Blueprint.from_json(blueprint.to_json())

    for op_dict in ops:
        op = op_dict.get("op", "")

        if op == "add_node":
            node_data = op_dict.get("node", {})
            if not node_data:
                continue
            node = _parse_node_dict(node_data)
            if node and node.id not in new_bp.nodes:
                new_bp.nodes[node.id] = node

        elif op == "replace_statement":
            nid = op_dict.get("id", "")
            if nid not in new_bp.nodes:
                continue
            node = new_bp.nodes[nid]
            # Never change the target node's lean_name or signature
            if nid == new_bp.target_id:
                continue
            if "signature" in op_dict:
                node.signature = op_dict["signature"]
                node.status = NodeStatus.PENDING  # reset
                node.proof = None
                node.proof_result = None
            if "nl_statement" in op_dict:
                node.nl_statement = op_dict["nl_statement"]
            if "proof_sketch" in op_dict:
                node.proof_sketch = op_dict["proof_sketch"]

        elif op == "decompose":
            nid = op_dict.get("id", "")
            if nid not in new_bp.nodes:
                continue
            # Add helper nodes
            helpers = op_dict.get("helpers", [])
            for h_data in helpers:
                helper = _parse_node_dict(h_data)
                if helper and helper.id not in new_bp.nodes:
                    new_bp.nodes[helper.id] = helper
            # Update the node's parents
            new_parents = op_dict.get("new_parents", [])
            if new_parents:
                existing_parents = list(new_bp.nodes[nid].parents)
                new_bp.nodes[nid].parents = list(set(existing_parents + new_parents))
                # If statement changed, reset
                new_bp.nodes[nid].status = NodeStatus.PENDING
                new_bp.nodes[nid].proof = None

        elif op == "rewire":
            nid = op_dict.get("id", "")
            if nid not in new_bp.nodes:
                continue
            if nid == new_bp.target_id:
                # Allow rewiring target's parents but not changing its signature
                pass
            new_parents = op_dict.get("parents", [])
            new_bp.nodes[nid].parents = new_parents

        elif op == "drop":
            nid = op_dict.get("id", "")
            if nid == new_bp.target_id:
                continue  # Never drop target
            if nid in new_bp.nodes:
                del new_bp.nodes[nid]
                # Remove from other nodes' parents
                for n in new_bp.nodes.values():
                    n.parents = [p for p in n.parents if p != nid]

    # Force target node to have canonical lean_name and signature
    if new_bp.target_id in new_bp.nodes:
        new_bp.nodes[new_bp.target_id].lean_name = target_lean_name
        new_bp.nodes[new_bp.target_id].signature = target_signature

    return new_bp


def _parse_node_dict(data: dict[str, Any]) -> Optional[BlueprintNode]:
    """Parse a node dict into a BlueprintNode."""
    node_id = data.get("id", "").strip()
    if not node_id:
        lean_name = data.get("lean_name", "")
        node_id = lean_name if lean_name else None
    if not node_id:
        return None

    kind_str = data.get("kind", "lemma").lower()
    if "def" in kind_str:
        kind = NodeKind.DEFINITION
    elif "theorem" in kind_str or "target" in kind_str:
        kind = NodeKind.TARGET
    else:
        kind = NodeKind.LEMMA

    definition_text: Optional[str] = None
    if kind == NodeKind.DEFINITION:
        raw_def_text = data.get("definition_text", "")
        if raw_def_text and str(raw_def_text).strip():
            definition_text = str(raw_def_text).strip()

    return BlueprintNode(
        id=node_id,
        kind=kind,
        lean_name=data.get("lean_name", node_id),
        signature=data.get("signature", ""),
        definition_text=definition_text,
        nl_statement=data.get("nl_statement", ""),
        proof_sketch=data.get("proof_sketch", ""),
        parents=data.get("parents", []),
        status=NodeStatus.PENDING,
    )


def _apply_proof_reuse(
    new_bp: Blueprint,
    old_bp: Blueprint,
) -> None:
    """For nodes with identical (lean_name, signature), reuse PROVED status."""
    proved_map: dict[tuple[str, str], Any] = {}
    for nid, node in old_bp.nodes.items():
        if node.status == NodeStatus.PROVED:
            key = (node.lean_name, node.signature)
            proved_map[key] = node

    for nid, node in new_bp.nodes.items():
        key = (node.lean_name, node.signature)
        if key in proved_map:
            old_node = proved_map[key]
            node.status = NodeStatus.PROVED
            node.proof = old_node.proof
            node.proof_result = old_node.proof_result
            node.iteration_proved = old_node.iteration_proved


class BlueprintRefiner:
    """Refines a blueprint based on proof attempt results."""

    def __init__(
        self,
        llm: "LLMClient",
        lean_client: "LeanClient",
        settings: "Settings",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._llm = llm
        self._lean_client = lean_client
        self._settings = settings
        self._logger = logger or get_logger(__name__)

    def refine(
        self,
        blueprint: Blueprint,
        iteration: int,
        target_lean_name: str = "",
        target_signature: str = "",
    ) -> Blueprint:
        """Refine the blueprint based on proof results.

        Args:
            blueprint: Current blueprint with proof results attached
            iteration: Current iteration number
            target_lean_name: Canonical target lean_name (for enforcement)
            target_signature: Canonical target signature (for enforcement)

        Returns:
            Refined blueprint (may be same as input if no changes needed)
        """
        # Get target info from blueprint if not provided
        if not target_lean_name and blueprint.target_id in blueprint.nodes:
            target_lean_name = blueprint.nodes[blueprint.target_id].lean_name
        if not target_signature and blueprint.target_id in blueprint.nodes:
            target_signature = blueprint.nodes[blueprint.target_id].signature

        state_text = _render_blueprint_for_refiner(blueprint)

        user_prompt = (
            f"## Blueprint state at iteration {iteration}\n\n"
            f"{state_text}\n\n"
            f"## Target theorem (MUST NOT CHANGE)\n"
            f"lean_name: `{target_lean_name}`\n"
            f"signature: `{target_signature}`\n\n"
            f"Please emit patch ops (or a full JSON blueprint) to fix the unproved nodes. "
            f"Address STATEMENT_WRONG by fixing statements; address PROOF_TOO_HARD by "
            f"adding helper lemmas. Do NOT change PROVED nodes."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": REFINER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        max_retries = self._settings.refiner_max_retries
        last_blueprint: Optional[Blueprint] = None

        for attempt in range(max_retries):
            self._logger.info(
                "BlueprintRefiner attempt %d/%d (iter %d)",
                attempt + 1, max_retries, iteration,
            )

            try:
                msg = self._llm.chat(
                    messages=messages,
                    tools=None,
                    enable_thinking=False,
                )
            except Exception as e:
                self._logger.error("LLM error in refiner: %s", e)
                break

            content = self._get_text_content(msg)
            messages.append({"role": "assistant", "content": content})

            if not content or "{" not in content:
                messages.append({
                    "role": "user",
                    "content": "Please emit the JSON patch ops or full blueprint JSON now.",
                })
                continue

            # Try to parse as patch ops first
            result_bp = self._try_patch_ops(
                content, blueprint, target_lean_name, target_signature
            )

            if result_bp is None:
                # Try full blueprint
                result_bp = self._try_full_blueprint(
                    content, target_lean_name, target_signature
                )

            if result_bp is None:
                error_msg = "Could not parse ops or full blueprint from response."
                messages.append({"role": "user", "content": error_msg})
                continue

            # Apply proof reuse
            _apply_proof_reuse(result_bp, blueprint)

            # Force target node
            if result_bp.target_id in result_bp.nodes:
                result_bp.nodes[result_bp.target_id].lean_name = target_lean_name
                result_bp.nodes[result_bp.target_id].signature = target_signature

            # Validate
            errors = result_bp.validate()
            fatal = [e for e in errors if "dead node" not in e and "not reachable" not in e]
            if fatal:
                error_msg = f"Refined blueprint has errors:\n" + "\n".join(fatal[:5])
                messages.append({"role": "user", "content": error_msg})
                last_blueprint = result_bp  # save as fallback
                continue

            # Skeleton compile
            skel_error = self._skeleton_compile(result_bp)
            if skel_error:
                error_msg = (
                    f"Skeleton type errors in refined blueprint:\n{skel_error}\n\n"
                    "Please fix signature errors and re-emit."
                )
                messages.append({"role": "user", "content": error_msg})
                last_blueprint = result_bp
                continue

            self._logger.info(
                "Refiner succeeded: %d nodes", len(result_bp.nodes)
            )
            return result_bp

        # Return last attempted or original blueprint
        if last_blueprint is not None:
            _apply_proof_reuse(last_blueprint, blueprint)
            self._logger.warning(
                "Refiner returning last partial blueprint after %d retries", max_retries
            )
            return last_blueprint

        self._logger.warning(
            "Refiner returning unchanged blueprint after %d retries", max_retries
        )
        return blueprint

    def _try_patch_ops(
        self,
        content: str,
        blueprint: Blueprint,
        target_lean_name: str,
        target_signature: str,
    ) -> Optional[Blueprint]:
        """Try to parse and apply patch ops."""
        json_str = _extract_json_object(content)
        if not json_str:
            return None
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        ops = data.get("ops", None)
        if ops is None:
            return None  # Not patch ops format

        if not isinstance(ops, list):
            return None

        try:
            return _apply_patch_ops(blueprint, ops, target_lean_name, target_signature)
        except Exception as e:
            self._logger.warning("Patch ops application failed: %s", e)
            return None

    def _try_full_blueprint(
        self,
        content: str,
        target_lean_name: str,
        target_signature: str,
    ) -> Optional[Blueprint]:
        """Try to parse a full blueprint JSON."""
        try:
            return parse_blueprint_json(content, target_lean_name, target_signature)
        except (ValueError, Exception) as e:
            self._logger.debug("Full blueprint parse failed: %s", e)
            return None

    def _skeleton_compile(self, blueprint: Blueprint) -> Optional[str]:
        """Type-check skeleton. Returns error string or None on success."""
        try:
            skeleton = build_skeleton_file(blueprint)
        except Exception as e:
            return f"Skeleton build error: {e}"
        try:
            check = self._lean_client.check(skeleton)
        except LeanServerError as e:
            self._logger.warning("Lean server error during skeleton compile: %s", e)
            return None
        if check.has_errors:
            error_lines = []
            for err in check.errors[:8]:
                pos = err.pos
                loc = f"L{pos.get('line', '?')}:C{pos.get('column', '?')}"
                error_lines.append(f"[{loc}] {err.data}")
            return "\n".join(error_lines)
        return None

    def _get_text_content(self, msg: Any) -> str:
        """Extract text content from LLM message."""
        content = getattr(msg, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if hasattr(p, "type") and getattr(p, "type") == "text":
                    parts.append(getattr(p, "text", ""))
                elif isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
            return " ".join(parts)
        return str(content)
