"""Blueprint generator agent."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from local_goedel.agents.parsers import parse_blueprint_json
from local_goedel.assembly.lean_assembler import build_skeleton_file, parse_theorem_file
from local_goedel.clients.lean_client import LeanServerError
from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.node import NodeStatus
from local_goedel.logging_utils import get_logger
from local_goedel.tools.base import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from local_goedel.clients.lean_client import LeanClient
    from local_goedel.clients.llm_client import LLMClient
    from local_goedel.config import Settings


BLUEPRINT_SYSTEM_PROMPT = """\
## Task
You are a Lean 4 proof architect. Given a Lean theorem signature, design a dependency \
graph (blueprint) for proving it. You do NOT write proofs — only plan the structure.

## Decomposition Guidelines
Plan a graph that captures the proof structure:
- Use **Definitions** for helper functions, sets, structures, or notation.
- Use **Lemmas** for intermediate facts requiring justification.
- Use exactly ONE **Theorem** — the main target, whose lean_name and signature MUST \
exactly match the input theorem.
- Each Lemma should be nearly trivial given its dependencies: at most 1-2 new logical \
ideas beyond its declared parents. If a step needs more, split it.
- Independent branches stay independent.
- Every statement is a closed, typed, standalone proposition: every variable carries an \
explicit quantifier and domain; every hypothesis appears as a premise.
- Every 'proof_sketch' field is a complete sketch citing each declared dependency by \
lean_name; show key equations; do NOT write "by algebra" or "obviously".
- Use snake_case identifiers derived from content, unique within the file.
- Declare nodes in topological order: Definitions first, Lemmas in dependency order, \
then the main Theorem last.
- The main theorem's lean_name and signature MUST match the input exactly.

## Output Format
Emit a single JSON object (no prose before or after the closing brace):
{
  "nodes": [
    {
      "id": "<unique_id>",
      "kind": "definition|lemma|theorem",
      "lean_name": "<snake_case_name>",
      "signature": "<type fragment after name, up to but NOT including ':= by'>",
      "nl_statement": "<natural language description>",
      "proof_sketch": "<detailed proof sketch citing parents by lean_name>",
      "parents": ["<id_of_dependency>", ...]
    },
    ...
  ],
  "target_id": "<id_of_the_main_theorem_node>"
}

## Skeleton Type-Check
After emitting the JSON, you may call the `lean_compile` tool with mode='full_file' to \
type-check the skeleton (all lemma/theorem bodies = sorry, definitions with real bodies). \
If you get type errors in statements, fix the signatures and re-emit. Iterate until the \
skeleton compiles or you run out of tool calls.

## Example
For `theorem foo (n : ℕ) : n + 0 = n`, a minimal blueprint has one node:
{"nodes": [{"id": "foo", "kind": "theorem", "lean_name": "foo",
  "signature": "(n : ℕ) : n + 0 = n", "nl_statement": "n + 0 = n for any natural n",
  "proof_sketch": "Follows from Nat.add_zero.", "parents": []}],
 "target_id": "foo"}
"""

BLUEPRINT_USER_TEMPLATE = """\
Design a dependency graph blueprint for the following Lean 4 theorem.

## Theorem file
```lean
{theorem_file_text}
```

## Target theorem
- lean_name: `{lean_name}`
- signature: `{signature}`
- Natural language: {nl_statement}
- Difficulty: {difficulty}

## Instructions
1. Emit a JSON blueprint with nodes in topological order (parents before children).
2. The main theorem node MUST have lean_name=`{lean_name}` and signature=`{signature}` exactly.
3. Each lemma should be easy given its parents — split if in doubt.
4. After emitting JSON, call lean_compile with mode='full_file' to type-check the skeleton.
5. Fix any type errors and re-emit the corrected JSON before calling lean_compile again.
6. When the skeleton compiles (or you decide it is good enough), stop.
"""


def _make_skeleton_tool_schema() -> dict[str, Any]:
    """OpenAI function schema for lean_compile (skeleton mode)."""
    return {
        "type": "function",
        "function": {
            "name": "lean_compile",
            "description": (
                "Compile Lean 4 code and return errors/warnings. "
                "Use mode='full_file' to submit a complete skeleton file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Lean code to check.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["proof_body", "full_file"],
                        "description": "How to interpret the code argument.",
                        "default": "full_file",
                    },
                },
                "required": ["code"],
            },
        },
    }


class BlueprintGenerator:
    """Generates a proof blueprint by prompting the LLM."""

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

    def generate(self, theorem_file_text: str, difficulty: str = "easy") -> Blueprint:
        """Generate a Blueprint from a theorem file.

        Args:
            theorem_file_text: Full content of the .lean theorem file
            difficulty: "easy" or "hard"

        Returns:
            Blueprint with all nodes PENDING
        """
        preamble, lean_name, signature, docstring = parse_theorem_file(theorem_file_text)

        user_prompt = BLUEPRINT_USER_TEMPLATE.format(
            theorem_file_text=theorem_file_text,
            lean_name=lean_name,
            signature=signature,
            nl_statement=docstring or "(no docstring)",
            difficulty=difficulty,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": BLUEPRINT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        tools = [_make_skeleton_tool_schema()]
        max_retries = self._settings.blueprint_max_retries
        last_error: Optional[str] = None
        last_blueprint: Optional[Blueprint] = None

        for attempt in range(max_retries):
            self._logger.info(
                "BlueprintGenerator attempt %d/%d", attempt + 1, max_retries
            )

            try:
                msg = self._llm.chat(
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    enable_thinking=False,  # tools + thinking conflict
                )
            except Exception as e:
                self._logger.error("LLM error in generator: %s", e)
                raise

            # Add assistant message to history
            assistant_dict = self._msg_to_dict(msg)
            messages.append(assistant_dict)

            # Check for tool calls (skeleton compile)
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                tool_results = self._handle_tool_calls(tool_calls, lean_name, signature)
                messages.extend(tool_results)
                # After tool results, continue loop to let LLM process
                continue

            # No tool calls — extract text content and parse blueprint
            content = self._get_text_content(msg)
            if not content:
                self._logger.warning("Empty content from LLM on attempt %d", attempt + 1)
                messages.append({
                    "role": "user",
                    "content": "Please emit the JSON blueprint now.",
                })
                continue

            # Try to parse the blueprint JSON
            parse_error: Optional[str] = None
            try:
                blueprint = parse_blueprint_json(content, lean_name, signature)
                # Validate
                validation_errors = blueprint.validate()
                fatal = [e for e in validation_errors if "dead node" not in e and "not reachable" not in e]
                if fatal:
                    parse_error = "Blueprint validation errors:\n" + "\n".join(fatal)
                else:
                    # Try skeleton compile
                    skeleton_error = self._skeleton_compile(blueprint)
                    if skeleton_error:
                        parse_error = f"Skeleton type errors:\n{skeleton_error}"
                    else:
                        # SUCCESS
                        self._logger.info(
                            "Blueprint generated: %d nodes", len(blueprint.nodes)
                        )
                        # Ensure all nodes are PENDING
                        for node in blueprint.nodes.values():
                            node.status = NodeStatus.PENDING
                        return blueprint
            except (ValueError, Exception) as e:
                parse_error = str(e)

            last_error = parse_error
            self._logger.warning(
                "Blueprint attempt %d failed: %s", attempt + 1, parse_error[:200]
            )

            # Feed error back for self-repair
            repair_msg = (
                f"The blueprint has issues:\n{parse_error}\n\n"
                "Please fix these issues and re-emit the complete corrected JSON blueprint. "
                f"Remember: the target theorem must have lean_name=`{lean_name}` "
                f"and signature=`{signature}` exactly."
            )
            messages.append({"role": "user", "content": repair_msg})

        # If we have a partial blueprint, return it anyway
        if last_blueprint is not None:
            self._logger.warning("Returning partial blueprint after %d retries", max_retries)
            return last_blueprint

        # Try one more time to extract a blueprint from last messages
        for msg_dict in reversed(messages):
            if msg_dict.get("role") == "assistant":
                content = msg_dict.get("content", "")
                if content and "{" in content:
                    try:
                        bp = parse_blueprint_json(content, lean_name, signature)
                        for node in bp.nodes.values():
                            node.status = NodeStatus.PENDING
                        return bp
                    except Exception:
                        pass

        raise RuntimeError(
            f"BlueprintGenerator failed after {max_retries} attempts. "
            f"Last error: {last_error}"
        )

    def _handle_tool_calls(
        self,
        tool_calls: list[Any],
        lean_name: str,
        signature: str,
    ) -> list[dict[str, Any]]:
        """Process lean_compile tool calls from the generator."""
        results = []
        for tc in tool_calls:
            tc_id = tc.id
            fn = tc.function
            try:
                args = json.loads(fn.arguments or "{}")
            except Exception:
                args = {}

            if fn.name == "lean_compile":
                code = args.get("code", "")
                result_content = self._compile_code(code)
            else:
                result_content = f"Unknown tool: {fn.name}"

            results.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result_content,
            })
        return results

    def _compile_code(self, code: str) -> str:
        """Compile Lean code and return a human-readable result."""
        if not code.strip():
            return "Error: empty code"
        try:
            check = self._lean_client.check(code)
        except LeanServerError as e:
            return f"Lean server error: {e}"

        if not check.has_errors:
            return "Skeleton compiled OK (no errors)."

        error_lines = []
        for err in check.errors[:10]:
            pos = err.pos
            loc = f"L{pos.get('line', '?')}:C{pos.get('column', '?')}"
            error_lines.append(f"[{loc}] {err.data}")
        return f"{len(check.errors)} error(s):\n" + "\n".join(error_lines)

    def _skeleton_compile(self, blueprint: Blueprint) -> Optional[str]:
        """Type-check the skeleton. Returns error string or None on success."""
        try:
            skeleton = build_skeleton_file(blueprint)
        except Exception as e:
            return f"Skeleton build error: {e}"

        try:
            check = self._lean_client.check(skeleton)
        except LeanServerError as e:
            self._logger.warning("Lean server error during skeleton compile: %s", e)
            return None  # Don't fail generation due to server issues

        if check.has_errors:
            error_lines = []
            for err in check.errors[:8]:
                pos = err.pos
                loc = f"L{pos.get('line', '?')}:C{pos.get('column', '?')}"
                error_lines.append(f"[{loc}] {err.data}")
            return "\n".join(error_lines)
        return None

    def _skeleton_compile_for_tool(self, blueprint: Blueprint) -> str:
        """Type-check skeleton and return human-readable result for tool output."""
        err = self._skeleton_compile(blueprint)
        if err is None:
            return "Skeleton compiled OK."
        return f"Skeleton errors:\n{err}"

    def _get_text_content(self, msg: Any) -> str:
        """Extract text content from an LLM message."""
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

    def _msg_to_dict(self, msg: Any) -> dict[str, Any]:
        """Convert OpenAI message to plain dict."""
        d: dict[str, Any] = {"role": "assistant"}
        content = getattr(msg, "content", None)
        if content is not None:
            d["content"] = content
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        return d
