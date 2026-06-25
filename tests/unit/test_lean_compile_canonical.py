"""Unit tests for LeanCompileTool mode='canonical' and _finalize fast-path.

All tests use stub/mock lean clients — no live Lean server required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from local_goedel.assembly.canonical import (
    CanonicalProblem,
    split_canonical,
)
from local_goedel.domain.lean_check import CheckResult, LeanMessage
from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus
from local_goedel.tools.base import ToolContext, ToolResult
from local_goedel.tools.lean_compile import LeanCompileTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_check(
    messages: list[LeanMessage],
    status: str = "",
    has_errors_override: bool = False,
) -> CheckResult:
    """Build a CheckResult from a list of LeanMessage objects."""
    return CheckResult(
        snippet_id="test",
        messages=messages,
        status=status,
    )


def _axiom_info(thm_name: str, axioms: list[str]) -> LeanMessage:
    axioms_str = ", ".join(axioms)
    return LeanMessage(
        severity="info",
        data=f"'{thm_name}' depends on axioms: [{axioms_str}]",
    )


def _error_msg(text: str, line: int = 1, col: int = 0) -> LeanMessage:
    return LeanMessage(
        severity="error",
        data=text,
        pos={"line": line, "column": col},
    )


SIMPLE_CANON = CanonicalProblem(
    header="import Mathlib",
    pre_decls="",
    thm_name="simple_thm",
    thm_signature="(a b : Real) : a ^ 2 + b ^ 2 >= 2 * a * b",
    keyword="theorem",
)


def _make_ctx(canon: Optional[CanonicalProblem] = None, lean_client: Any = None) -> ToolContext:
    node = BlueprintNode(
        id="t1",
        lean_name="simple_thm",
        signature="(a b : Real) : a ^ 2 + b ^ 2 >= 2 * a * b",
        kind=NodeKind.TARGET,
    )
    mock_lean = lean_client or MagicMock()
    return ToolContext(
        lean_client=mock_lean,
        mathlib_client=MagicMock(),
        target_node=node,
        parent_nodes=[],
        settings=MagicMock(),
        logger=MagicMock(),
        canonical=canon,
    )


# ---------------------------------------------------------------------------
# Tests: canonical mode — safeguard violations (no Lean call needed)
# ---------------------------------------------------------------------------

class TestCanonicalSafeguardViolations:
    """Violations caught before the Lean call (fast-fail)."""

    def test_sorry_in_body_rejected(self):
        """A body containing 'sorry' is rejected immediately."""
        tool = LeanCompileTool()
        ctx = _make_ctx(SIMPLE_CANON)

        result = tool.run({"code": "by sorry", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "SAFEGUARD VIOLATION" in result.content
        assert "sorry" in result.content
        # No Lean call should be made.
        ctx.lean_client.check.assert_not_called()

    def test_native_decide_in_body_rejected(self):
        """native_decide is caught before Lean call."""
        tool = LeanCompileTool()
        ctx = _make_ctx(SIMPLE_CANON)

        result = tool.run({"code": "by native_decide", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "SAFEGUARD VIOLATION" in result.content
        ctx.lean_client.check.assert_not_called()

    def test_injected_theorem_at_col0_rejected(self):
        """A top-level 'theorem' at col 0 inside the body is a safeguard violation."""
        tool = LeanCompileTool()
        ctx = _make_ctx(SIMPLE_CANON)

        body = "by\n  rfl\ntheorem injected : True := trivial\n"
        result = tool.run({"code": body, "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "SAFEGUARD VIOLATION" in result.content
        ctx.lean_client.check.assert_not_called()

    def test_no_canonical_context_returns_error(self):
        """canonical=None on ctx → ok=False, descriptive message."""
        tool = LeanCompileTool()
        ctx = _make_ctx(canon=None)  # no canonical

        result = tool.run({"code": "by rfl", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "canonical mode requires" in result.content


# ---------------------------------------------------------------------------
# Tests: canonical mode — Lean call results
# ---------------------------------------------------------------------------

class TestCanonicalLeanResults:
    """Behaviour when the body is clean and Lean is called."""

    def test_proof_complete_on_clean_whitelisted(self):
        """Clean proof with only allowed axioms → PROOF COMPLETE."""
        tool = LeanCompileTool()

        # Stub Lean client returns a valid check with whitelisted axioms.
        clean_check = _make_check([
            _axiom_info("simple_thm", ["propext", "Classical.choice", "Quot.sound"])
        ], status="valid")
        stub_lean = MagicMock()
        stub_lean.check.return_value = clean_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        result = tool.run({"code": "by nlinarith [sq_nonneg (a - b)]", "mode": "canonical"}, ctx)

        assert result.ok is True
        assert "PROOF COMPLETE" in result.content
        assert "canonical" in result.content
        assert "axioms OK" in result.content

    def test_axiom_violation_on_ofReduceBool(self):
        """Lean.ofReduceBool in axioms → AXIOM VIOLATION, ok=False."""
        tool = LeanCompileTool()

        bad_check = _make_check([
            _axiom_info("simple_thm", ["propext", "Lean.ofReduceBool"])
        ], status="valid")
        stub_lean = MagicMock()
        stub_lean.check.return_value = bad_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        result = tool.run({"code": "by decide", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "AXIOM VIOLATION" in result.content
        assert "Lean.ofReduceBool" in result.content

    def test_sorry_status_not_accepted(self):
        """status='sorry' → not accepted (ok=False)."""
        tool = LeanCompileTool()

        sorry_check = _make_check([
            LeanMessage(severity="warning", data="declaration uses 'sorry'")
        ], status="sorry")
        stub_lean = MagicMock()
        stub_lean.check.return_value = sorry_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        result = tool.run({"code": "by nlinarith", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "sorry" in result.content.lower()

    def test_compile_error_returns_ok_false(self):
        """Lean compilation errors → ok=False with error lines."""
        tool = LeanCompileTool()

        err_check = _make_check([
            _error_msg("unknown tactic 'nlinarithx'", line=3, col=4)
        ], status="lean_error")
        stub_lean = MagicMock()
        stub_lean.check.return_value = err_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        result = tool.run({"code": "by nlinarithx", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "error" in result.content.lower()
        assert "nlinarithx" in result.content

    def test_timeout_error_returns_ok_false(self):
        """status='timeout_error' → ok=False, timeout message."""
        tool = LeanCompileTool()

        timeout_check = _make_check([], status="timeout_error")
        stub_lean = MagicMock()
        stub_lean.check.return_value = timeout_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        result = tool.run({"code": "by decide", "mode": "canonical"}, ctx)

        assert result.ok is False
        assert "timed out" in result.content.lower()

    def test_ctx_last_check_is_set(self):
        """ctx.last_check is populated after a Lean call."""
        tool = LeanCompileTool()

        clean_check = _make_check([
            _axiom_info("simple_thm", ["propext"])
        ], status="valid")
        stub_lean = MagicMock()
        stub_lean.check.return_value = clean_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        assert ctx.last_check is None

        tool.run({"code": "by ring", "mode": "canonical"}, ctx)

        assert ctx.last_check is clean_check

    def test_attempts_counter_incremented(self):
        """ctx.attempts is incremented by canonical mode calls."""
        tool = LeanCompileTool()

        clean_check = _make_check([_axiom_info("simple_thm", [])], status="valid")
        stub_lean = MagicMock()
        stub_lean.check.return_value = clean_check

        ctx = _make_ctx(SIMPLE_CANON, lean_client=stub_lean)
        assert ctx.attempts == 0

        tool.run({"code": "by ring", "mode": "canonical"}, ctx)
        assert ctx.attempts == 1

        tool.run({"code": "by ring", "mode": "canonical"}, ctx)
        assert ctx.attempts == 2


# ---------------------------------------------------------------------------
# Tests: existing modes unaffected
# ---------------------------------------------------------------------------

class TestExistingModesUnchanged:
    """proof_body and full_file modes still work as before."""

    def test_proof_body_mode_works(self):
        """mode='proof_body' still assembles via build_node_file and calls Lean."""
        tool = LeanCompileTool()

        clean_check = _make_check([], status="valid")
        stub_lean = MagicMock()
        stub_lean.check.return_value = clean_check

        ctx = _make_ctx(lean_client=stub_lean)
        # canonical is None — that's fine for proof_body mode.
        ctx.canonical = None

        result = tool.run({"code": "by rfl", "mode": "proof_body"}, ctx)

        # Lean was called (unlike safeguard rejection).
        stub_lean.check.assert_called_once()
        # Result is ok because status=valid and no sorry.
        assert result.ok is True
        assert "PROOF COMPLETE" in result.content

    def test_full_file_mode_passes_code_verbatim(self):
        """mode='full_file' passes the code directly to Lean."""
        tool = LeanCompileTool()

        clean_check = _make_check([], status="valid")
        stub_lean = MagicMock()
        stub_lean.check.return_value = clean_check

        ctx = _make_ctx(lean_client=stub_lean)
        ctx.canonical = None

        verbatim = "import Mathlib\ntheorem t : 1 = 1 := by rfl\n#print axioms t\n"
        tool.run({"code": verbatim, "mode": "full_file"}, ctx)

        call_args = stub_lean.check.call_args[0][0]
        assert call_args == verbatim


# ---------------------------------------------------------------------------
# Tests: ToolContext.canonical field
# ---------------------------------------------------------------------------

class TestToolContextCanonicalField:
    """Verify the new field on ToolContext."""

    def test_canonical_defaults_to_none(self):
        node = BlueprintNode(
            id="x", lean_name="foo", signature=": True", kind=NodeKind.TARGET
        )
        ctx = ToolContext(
            lean_client=None,
            mathlib_client=None,
            target_node=node,
            parent_nodes=[],
            settings=None,
            logger=MagicMock(),
        )
        assert ctx.canonical is None

    def test_canonical_can_be_set(self):
        node = BlueprintNode(
            id="x", lean_name="foo", signature=": True", kind=NodeKind.TARGET
        )
        canon = CanonicalProblem(
            header="import Mathlib",
            pre_decls="",
            thm_name="foo",
            thm_signature=": True",
        )
        ctx = ToolContext(
            lean_client=None,
            mathlib_client=None,
            target_node=node,
            parent_nodes=[],
            settings=None,
            logger=MagicMock(),
            canonical=canon,
        )
        assert ctx.canonical is canon
        assert ctx.canonical.thm_name == "foo"


# ---------------------------------------------------------------------------
# Tests: _finalize fast-path (stub pipeline)
# ---------------------------------------------------------------------------

class TestFinalizeFastPath:
    """Unit test for the fast-path in Pipeline._finalize via stub lean client."""

    def _make_pipeline_stub(self, check_result: CheckResult):
        """Build a minimal Pipeline-like object to test _try_canonical_body."""
        from local_goedel.orchestrator.pipeline import Pipeline

        # Patch lean client on instance after creation to avoid real connections.
        settings = MagicMock()
        settings.runs_dir = "runs"
        settings.prover_max_turns = 5
        settings.prover_max_tool_calls = 5
        settings.default_max_iter = 1
        settings.max_wall_s = 3600

        stub_lean = MagicMock()
        stub_lean.check.return_value = check_result

        # We need to avoid hitting real servers during __init__.
        # Patch at the class/module level temporarily.
        import unittest.mock as mock
        with mock.patch("local_goedel.orchestrator.pipeline.LeanClient") as MockLC, \
             mock.patch("local_goedel.orchestrator.pipeline.MathlibSearchClient") as MockMC, \
             mock.patch("local_goedel.orchestrator.pipeline.LLMClient") as MockLLC, \
             mock.patch("local_goedel.orchestrator.pipeline.BlueprintGenerator"), \
             mock.patch("local_goedel.orchestrator.pipeline.BlueprintRefiner"):
            MockLC.return_value = stub_lean
            MockMC.return_value = MagicMock()
            MockLLC.return_value = MagicMock()
            pipeline = Pipeline(settings)

        # Replace the lean client with our stub on the instance.
        pipeline._lean_client = stub_lean
        return pipeline

    def test_try_canonical_body_success(self):
        """_try_canonical_body returns (code, check) when Lean says valid + clean axioms."""
        clean_check = _make_check([
            _axiom_info("simple_thm", ["propext", "Classical.choice", "Quot.sound"])
        ], status="valid")

        pipeline = self._make_pipeline_stub(clean_check)

        result = pipeline._try_canonical_body(SIMPLE_CANON, "by nlinarith [sq_nonneg (a-b)]", "simple_thm")

        assert result is not None
        final_code, check = result
        assert "theorem simple_thm" in final_code
        assert check is clean_check

    def test_try_canonical_body_fails_on_errors(self):
        """_try_canonical_body returns None when Lean reports errors."""
        err_check = _make_check([
            _error_msg("type mismatch", line=1, col=0)
        ], status="lean_error")

        pipeline = self._make_pipeline_stub(err_check)

        result = pipeline._try_canonical_body(SIMPLE_CANON, "by ring", "simple_thm")
        assert result is None

    def test_try_canonical_body_fails_on_sorry_body(self):
        """_try_canonical_body returns None immediately if body contains sorry."""
        # Even a valid-looking check should not matter since scan catches it first.
        clean_check = _make_check([], status="valid")
        pipeline = self._make_pipeline_stub(clean_check)

        result = pipeline._try_canonical_body(SIMPLE_CANON, "by sorry", "simple_thm")
        assert result is None
        # Lean should NOT be called because scan_proof_body catches it first.
        pipeline._lean_client.check.assert_not_called()

    def test_try_canonical_body_fails_on_axiom_violation(self):
        """_try_canonical_body returns None when axiom whitelist fails."""
        bad_check = _make_check([
            _axiom_info("simple_thm", ["propext", "Lean.ofReduceBool"])
        ], status="valid")
        pipeline = self._make_pipeline_stub(bad_check)

        result = pipeline._try_canonical_body(SIMPLE_CANON, "by decide", "simple_thm")
        assert result is None
