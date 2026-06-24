"""Lean file assembly utilities."""
from __future__ import annotations

import re
from typing import Optional

from local_goedel.domain.node import BlueprintNode, NodeKind


def render_decl(lean_name: str, signature: str, body_or_none: Optional[str]) -> str:
    """Render a theorem/lemma declaration.

    Args:
        lean_name: The theorem name
        signature: Type fragment AFTER name, WITHOUT ':= by ...'
        body_or_none: Proof body, or None to use 'by sorry'

    Returns:
        Complete theorem declaration string
    """
    if body_or_none is None:
        body = "by sorry"
    else:
        body = body_or_none
    return f"theorem {lean_name} {signature} := {body}"


def render_definition(node: BlueprintNode) -> str:
    """Render a DEFINITION node as its verbatim Lean declaration.

    Priority order:
    1. node.definition_text — verbatim full declaration (preferred)
    2. node.signature already contains ':=' — emit 'def <name> <sig>'
    3. Fall back to 'def <name> <sig>' with a placeholder sorry body

    Args:
        node: A BlueprintNode with kind == DEFINITION

    Returns:
        Complete Lean definition declaration string
    """
    # Priority 1: explicit full declaration stored in definition_text
    if node.definition_text:
        return node.definition_text.strip()

    sig = node.signature.strip()

    # Priority 2: signature already embeds ':= ...' body
    if ":=" in sig:
        return f"def {node.lean_name} {sig}"

    # Priority 3: no body available — emit a sorry placeholder so the
    # skeleton still type-checks.  This is a best-effort fallback;
    # ideally the LLM supplies definition_text or a signature with ':='.
    return f"def {node.lean_name} {sig} := by sorry"


def _render_node_as_parent(parent: BlueprintNode) -> str:
    """Render a parent node for inclusion in another node's proof file.

    DEFINITION nodes emit their real declaration (def/abbrev/etc.).
    LEMMA/TARGET nodes emit a theorem sorry stub if not proved, or their
    real proof if they have been proved.
    """
    from local_goedel.domain.node import NodeStatus
    if parent.kind == NodeKind.DEFINITION:
        return render_definition(parent)
    if parent.status == NodeStatus.PROVED and parent.proof is not None:
        return render_decl(parent.lean_name, parent.signature, parent.proof)
    return render_decl(parent.lean_name, parent.signature, None)


def build_node_file(
    target_node: BlueprintNode,
    parent_nodes: list[BlueprintNode],
    proof_body: str,
    add_axiom_print: bool = True,
) -> str:
    """Build a complete Lean file for checking a node's proof.

    Args:
        target_node: The theorem node being proved
        parent_nodes: Parent nodes (dependencies).  DEFINITION parents are
            rendered as their real declaration; LEMMA parents are sorry stubs
            (or their real proof if already proved).
        proof_body: The proof body to use for target_node
        add_axiom_print: Whether to add #print axioms at the end

    Returns:
        Complete Lean file content
    """
    lines = ["import Mathlib", ""]

    for parent in parent_nodes:
        lines.append(_render_node_as_parent(parent))

    if parent_nodes:
        lines.append("")

    # Render target with actual proof body (target is never a DEFINITION)
    lines.append(render_decl(target_node.lean_name, target_node.signature, proof_body))

    if add_axiom_print:
        lines.append("")
        lines.append(f"#print axioms {target_node.lean_name}")

    return "\n".join(lines) + "\n"


def build_final_file(
    ordered_nodes: list[BlueprintNode],
    target_name: str,
) -> str:
    """Build a self-contained final file with all proved nodes.

    DEFINITION nodes are emitted as their real Lean declaration.
    LEMMA/TARGET nodes use their proof body (or 'by sorry' as fallback).

    Args:
        ordered_nodes: Nodes in dependency order (parents before children)
        target_name: The main target theorem name

    Returns:
        Complete Lean file content
    """
    lines = ["import Mathlib", ""]

    for node in ordered_nodes:
        if node.kind == NodeKind.DEFINITION:
            lines.append(render_definition(node))
        else:
            body = node.proof if node.proof else "by sorry"
            lines.append(render_decl(node.lean_name, node.signature, body))
        lines.append("")

    lines.append(f"#print axioms {target_name}")
    return "\n".join(lines) + "\n"


def build_skeleton_file(blueprint: "Blueprint") -> str:  # type: ignore[name-defined]
    """Build a skeleton Lean file for type-checking blueprint statements.

    DEFINITION nodes are rendered as their real Lean declarations.
    LEMMA/TARGET nodes get 'by sorry' stubs so type signatures are checked
    without requiring real proofs.

    Args:
        blueprint: The Blueprint to render

    Returns:
        Complete Lean file with real definitions and sorry stubs for proofs
    """
    from local_goedel.domain.blueprint import Blueprint  # noqa: F811

    try:
        order = blueprint.topo_sort()
    except ValueError:
        order = list(blueprint.nodes.keys())

    lines = ["import Mathlib", ""]

    for nid in order:
        node = blueprint.nodes[nid]
        if node.kind == NodeKind.DEFINITION:
            lines.append(render_definition(node))
        else:
            # Lemma or Target: use sorry stub
            lines.append(render_decl(node.lean_name, node.signature, None))
        lines.append("")

    return "\n".join(lines) + "\n"


def parse_theorem_file(text: str) -> tuple[list[str], str, str, str]:
    """Parse a Lean theorem file to extract its components.

    LIMITATION: Only single-theorem files are supported. Files with multiple
    ``theorem``/``lemma`` declarations will raise ``ValueError`` so the caller
    can skip or handle them explicitly rather than silently prove only the first
    theorem and ignore the rest.

    Args:
        text: Full content of the Lean file

    Returns:
        Tuple of (preamble_lines, lean_name, signature_fragment, docstring)
        - preamble_lines: import/set_options/open lines
        - lean_name: name of the theorem
        - signature_fragment: everything after name up to ':= by' (exclusive)
        - docstring: NL description from /-- ... -/ comment if present

    Raises:
        ValueError: if the file contains more than one theorem/lemma declaration.
    """
    lines = text.splitlines()

    # --- Multi-theorem guard ---
    # Count top-level theorem/lemma/def lines (not inside comments or strings).
    # A simple heuristic: count non-indented lines matching the pattern.
    _thm_decls = [
        ln for ln in lines
        if re.match(r'^(theorem|lemma)\s+\S+', ln.strip())
    ]
    if len(_thm_decls) > 1:
        names = [re.match(r'^(?:theorem|lemma)\s+(\S+)', ln.strip()).group(1)  # type: ignore[union-attr]
                 for ln in _thm_decls]
        raise ValueError(
            f"parse_theorem_file: file contains {len(_thm_decls)} theorem/lemma "
            f"declarations ({', '.join(names)}). "
            "Multi-theorem files are not supported - run each theorem separately."
        )
    preamble: list[str] = []
    docstring = ""
    lean_name = ""
    signature_fragment = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Collect preamble lines
        if (stripped.startswith("import ")
                or stripped.startswith("set_option")
                or stripped.startswith("open ")):
            preamble.append(line)
            i += 1
            continue

        # Doc comment
        if stripped.startswith("/--"):
            first_content = stripped[3:].strip()
            # Single-line doc comment: /-- ... -/
            if first_content.endswith("-/"):
                doc_lines = [first_content[:-2].strip()]
            else:
                doc_lines = [first_content]
                i += 1
                while i < len(lines):
                    dl = lines[i].strip()
                    if dl.endswith("-/"):
                        inner = dl[:-2].strip()
                        if inner:
                            doc_lines.append(inner)
                        break
                    doc_lines.append(dl)
                    i += 1
            docstring = " ".join(doc_lines).strip()
            i += 1
            continue

        # Theorem declaration
        thm_match = re.match(r'(theorem|lemma|def)\s+(\S+)\s*(.*)', stripped)
        if thm_match:
            lean_name = thm_match.group(2)
            rest = thm_match.group(3)

            # Collect multi-line signature
            full_sig = rest
            while i + 1 < len(lines):
                if ":= by" in full_sig or ":=" in full_sig:
                    break
                i += 1
                full_sig += " " + lines[i].strip()

            # Strip ':= by ...' or ':= ...'
            for sep in (":= by", ":="):
                if sep in full_sig:
                    full_sig = full_sig[:full_sig.index(sep)].strip()
                    break

            signature_fragment = full_sig.strip()
            break

        i += 1

    return preamble, lean_name, signature_fragment, docstring
