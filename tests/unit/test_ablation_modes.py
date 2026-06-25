"""Unit tests for ablation modes (Change 4).

Tests:
- Settings.mode field and validate_mode
- Pipeline.run() dispatches to the correct private method per mode
- Unknown mode raises ValueError
- oneshot body extraction from various response shapes
- oneshot gate rejects bodies that fail scan_proof_body
- RunOutcome.mode is set correctly
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from local_goedel.assembly.canonical import (
    CanonicalProblem,
    build_canonical_submission,
    scan_proof_body,
)
from local_goedel.config import Settings, load_settings, validate_mode
from local_goedel.orchestrator.pipeline import (
    _extract_body_from_response,
    _extract_text,
    _strip_theorem_decl,
)
from local_goedel.orchestrator.state import RunOutcome


# =============================================================================
# validate_mode
# =============================================================================

class TestValidateMode:
    def test_full_accepted(self):
        assert validate_mode("full") == "full"

    def test_tool_loop_accepted(self):
        assert validate_mode("tool_loop") == "tool_loop"

    def test_oneshot_accepted(self):
        assert validate_mode("oneshot") == "oneshot"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown pipeline mode"):
            validate_mode("bad_mode")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Unknown pipeline mode"):
            validate_mode("")

    def test_case_sensitive(self):
        with pytest.raises(ValueError):
            validate_mode("Full")


# =============================================================================
# Settings.mode field
# =============================================================================

class TestSettingsMode:
    def test_default_mode_is_full(self):
        s = Settings()
        assert s.mode == "full"

    def test_mode_can_be_set(self):
        s = Settings(mode="tool_loop")
        assert s.mode == "tool_loop"

    def test_oneshot_retries_default(self):
        s = Settings()
        assert s.oneshot_retries == 1

    def test_load_settings_mode_env(self, monkeypatch, tmp_path):
        """MODE env var is picked up by load_settings."""
        monkeypatch.setenv("MODE", "oneshot")
        s = load_settings(env_path=tmp_path / "nonexistent.env")
        assert s.mode == "oneshot"

    def test_load_settings_mode_env_file(self, tmp_path):
        """MODE in .env file is picked up."""
        env_file = tmp_path / ".env"
        env_file.write_text('$env:MODE = "tool_loop"\n', encoding="utf-8")
        s = load_settings(env_path=env_file)
        assert s.mode == "tool_loop"


# =============================================================================
# RunOutcome.mode field
# =============================================================================

class TestRunOutcomeMode:
    def test_mode_field_default_empty(self):
        o = RunOutcome(success=True)
        assert o.mode == ""

    def test_mode_field_can_be_set(self):
        o = RunOutcome(success=True, mode="tool_loop")
        assert o.mode == "tool_loop"


# =============================================================================
# Pipeline.run() mode dispatch (monkeypatched)
# =============================================================================

class TestPipelineDispatch:
    """Verify that Pipeline.run routes to the correct private method."""

    def _make_pipeline(self, mode: str = "full") -> "Pipeline":  # type: ignore[name-defined]
        from local_goedel.orchestrator.pipeline import Pipeline

        settings = Settings(
            api_key="test-key",
            mode=mode,
            runs_dir=str(Path("/tmp/test_runs")),
        )
        # Patch all clients so __init__ doesn't try to connect.
        with (
            patch("local_goedel.orchestrator.pipeline.LeanClient"),
            patch("local_goedel.orchestrator.pipeline.MathlibSearchClient"),
            patch("local_goedel.orchestrator.pipeline.LLMClient"),
            patch("local_goedel.orchestrator.pipeline.Prover"),
            patch("local_goedel.orchestrator.pipeline.Synthesizer"),
            patch("local_goedel.orchestrator.pipeline.BlueprintGenerator"),
            patch("local_goedel.orchestrator.pipeline.BlueprintRefiner"),
        ):
            return Pipeline(settings)

    def _run_with_stubs(self, pipeline, mode: str, tmp_path: Path):
        """Call pipeline.run() with all private helpers stubbed."""
        # Write a trivial .lean file
        lean_file = tmp_path / "t.lean"
        lean_file.write_text(
            "import Mathlib\ntheorem t (a b : Int) : a + b = b + a := by sorry\n",
            encoding="utf-8",
        )

        stub_outcome = RunOutcome(success=True, reason="stubbed")

        calls: dict[str, int] = {"full": 0, "tool_loop": 0, "oneshot": 0}

        def fake_full(*a, **kw):
            calls["full"] += 1
            return stub_outcome

        def fake_tool_loop(*a, **kw):
            calls["tool_loop"] += 1
            return stub_outcome

        def fake_oneshot(*a, **kw):
            calls["oneshot"] += 1
            return stub_outcome

        pipeline._run_full = fake_full
        pipeline._run_tool_loop = fake_tool_loop
        pipeline._run_oneshot = fake_oneshot

        # Also stub ArtifactWriter so nothing is written to disk
        with (
            patch("local_goedel.orchestrator.pipeline.make_run_id", return_value="t_000"),
            patch("local_goedel.orchestrator.pipeline.ArtifactWriter") as mock_aw,
        ):
            mock_aw.return_value.log_path.return_value = tmp_path / "run.log"
            mock_aw.return_value.save_config = MagicMock()
            mock_aw.return_value.save_theorem = MagicMock()
            mock_aw.return_value.save_outcome = MagicMock()

            # Pipeline.run now uses current_run_logfile (ContextVar) instead of
            # logging.FileHandler, so no FileHandler patch is needed.
            outcome = pipeline.run(
                theorem_file_path=lean_file,
                mode=mode,
            )

        return outcome, calls

    def test_dispatch_full(self, tmp_path):
        pipeline = self._make_pipeline("full")
        outcome, calls = self._run_with_stubs(pipeline, "full", tmp_path)
        assert calls["full"] == 1
        assert calls["tool_loop"] == 0
        assert calls["oneshot"] == 0
        assert outcome.mode == "full"

    def test_dispatch_tool_loop(self, tmp_path):
        pipeline = self._make_pipeline("tool_loop")
        outcome, calls = self._run_with_stubs(pipeline, "tool_loop", tmp_path)
        assert calls["tool_loop"] == 1
        assert calls["full"] == 0
        assert calls["oneshot"] == 0
        assert outcome.mode == "tool_loop"

    def test_dispatch_oneshot(self, tmp_path):
        pipeline = self._make_pipeline("oneshot")
        outcome, calls = self._run_with_stubs(pipeline, "oneshot", tmp_path)
        assert calls["oneshot"] == 1
        assert calls["full"] == 0
        assert calls["tool_loop"] == 0
        assert outcome.mode == "oneshot"

    def test_mode_param_overrides_settings(self, tmp_path):
        """A 'mode' kwarg to run() overrides the settings default."""
        pipeline = self._make_pipeline("full")
        outcome, calls = self._run_with_stubs(pipeline, "tool_loop", tmp_path)
        assert calls["tool_loop"] == 1
        assert calls["full"] == 0

    def test_unknown_mode_raises(self, tmp_path):
        pipeline = self._make_pipeline("full")
        lean_file = tmp_path / "t.lean"
        lean_file.write_text(
            "import Mathlib\ntheorem t : True := by trivial\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Unknown pipeline mode"):
            with (
                patch("local_goedel.orchestrator.pipeline.make_run_id", return_value="t_000"),
                patch("local_goedel.orchestrator.pipeline.ArtifactWriter") as mock_aw,
            ):
                mock_aw.return_value.log_path.return_value = tmp_path / "run.log"
                mock_aw.return_value.save_config = MagicMock()
                mock_aw.return_value.save_theorem = MagicMock()
                mock_aw.return_value.save_outcome = MagicMock()
                pipeline.run(theorem_file_path=lean_file, mode="bogus")


# =============================================================================
# _extract_text helper
# =============================================================================

class TestExtractText:
    def test_string_content(self):
        msg = MagicMock()
        msg.content = "hello world"
        assert _extract_text(msg) == "hello world"

    def test_none_content(self):
        msg = MagicMock()
        msg.content = None
        assert _extract_text(msg) == ""

    def test_list_of_strings(self):
        msg = MagicMock()
        msg.content = ["foo", "bar"]
        result = _extract_text(msg)
        assert "foo" in result
        assert "bar" in result

    def test_list_of_blocks_with_text_attr(self):
        block = MagicMock()
        block.text = "proof here"
        msg = MagicMock()
        msg.content = [block]
        assert "proof here" in _extract_text(msg)


# =============================================================================
# _extract_body_from_response and _strip_theorem_decl
# =============================================================================

class TestExtractBodyFromResponse:
    """Tests for oneshot proof-body extraction logic."""

    # --- ```lean fenced block ---

    def test_lean_fenced_block_simple(self):
        raw = "Here is the proof:\n```lean\nby nlinarith\n```\nDone."
        body = _extract_body_from_response(raw)
        assert body == "by nlinarith"

    def test_lean_fenced_block_multiline(self):
        raw = (
            "```lean\n"
            "by\n"
            "  have h := sq_nonneg (a - b)\n"
            "  nlinarith [h]\n"
            "```"
        )
        body = _extract_body_from_response(raw)
        assert body is not None
        assert "have h" in body
        assert "nlinarith" in body

    def test_lean_fenced_block_with_full_theorem_stripped(self):
        """If the ```lean block contains a full theorem, strip the declaration."""
        raw = (
            "```lean\n"
            "theorem myThm (a b : ℝ) : a^2 + b^2 ≥ 0 :=\n"
            "  by positivity\n"
            "```"
        )
        body = _extract_body_from_response(raw)
        assert body is not None
        # Should be just the body part, no theorem keyword
        assert "theorem" not in body
        assert "positivity" in body

    def test_lean_fenced_block_by_only(self):
        raw = "```lean\nby ring\n```"
        body = _extract_body_from_response(raw)
        assert body == "by ring"

    # --- Generic ``` fenced block ---

    def test_generic_fenced_block(self):
        raw = "```\nby linarith\n```"
        body = _extract_body_from_response(raw)
        assert body == "by linarith"

    # --- Full theorem declaration response (no fenced block) ---

    def test_full_theorem_no_fence(self):
        raw = (
            "theorem myThm (a b : ℝ) : a + b = b + a :=\n"
            "  by ring"
        )
        body = _extract_body_from_response(raw)
        assert body is not None
        assert "ring" in body
        # Should not contain the declaration line
        assert "theorem" not in body

    def test_full_theorem_by_sorry_stripped(self):
        """Full theorem ending with ':= by sorry' should yield 'by sorry' as body."""
        raw = "theorem t (a : ℝ) : a = a := by rfl"
        body = _extract_body_from_response(raw)
        assert body is not None
        assert "rfl" in body

    # --- Already-clean body (starts with 'by') ---

    def test_plain_by_body(self):
        raw = "by nlinarith [sq_nonneg (a - b)]"
        body = _extract_body_from_response(raw)
        assert body == "by nlinarith [sq_nonneg (a - b)]"

    # --- Empty / None ---

    def test_empty_string(self):
        assert _extract_body_from_response("") is None

    def test_whitespace_only(self):
        assert _extract_body_from_response("   \n  ") is None


# =============================================================================
# _strip_theorem_decl edge cases
# =============================================================================

class TestStripTheoremDecl:
    def test_no_decl_returns_unchanged(self):
        body = "by nlinarith"
        assert _strip_theorem_decl(body) == "by nlinarith"

    def test_empty_returns_none(self):
        assert _strip_theorem_decl("") is None

    def test_theorem_with_assign(self):
        text = "theorem t (a : ℝ) : a ≥ 0 := by positivity"
        result = _strip_theorem_decl(text)
        assert result is not None
        assert "positivity" in result
        assert "theorem" not in result

    def test_lemma_keyword(self):
        text = "lemma helper (n : ℕ) : n + 0 = n := by simp"
        result = _strip_theorem_decl(text)
        assert result is not None
        assert "simp" in result
        assert "lemma" not in result

    def test_multiline_proof(self):
        text = (
            "theorem big (a b : ℝ) : a^2 + b^2 ≥ 0 :=\n"
            "  by\n"
            "    have h := sq_nonneg a\n"
            "    linarith [sq_nonneg b]"
        )
        result = _strip_theorem_decl(text)
        assert result is not None
        assert "have h" in result
        assert "theorem" not in result


# =============================================================================
# oneshot gate: scan_proof_body rejection
# =============================================================================

class TestOneshotGateViaCanonical:
    """Test the logical flow of the oneshot gate without live Lean server."""

    CANON = CanonicalProblem(
        header="import Mathlib",
        pre_decls="",
        thm_name="trivial_t",
        thm_signature="(a b : ℝ) : a^2 + b^2 ≥ 0",
        keyword="theorem",
    )

    def test_clean_body_passes_scan(self):
        body = "by nlinarith [sq_nonneg a, sq_nonneg b]"
        violations = scan_proof_body(body)
        assert violations == []

    def test_sorry_body_rejected_by_scan(self):
        body = "by\n  intro h\n  sorry\n"
        violations = scan_proof_body(body)
        assert any("sorry" in v for v in violations)

    def test_native_decide_rejected_by_scan(self):
        body = "by native_decide"
        violations = scan_proof_body(body)
        assert any("native_decide" in v for v in violations)

    def test_injected_theorem_rejected_by_scan(self):
        body = "by\n  rfl\ntheorem injected : True := trivial\n"
        violations = scan_proof_body(body)
        assert any("theorem" in v for v in violations)

    def test_build_canonical_contains_body(self):
        """build_canonical_submission uses canon header + body correctly."""
        body = "by nlinarith [sq_nonneg a, sq_nonneg b]"
        final = build_canonical_submission(self.CANON, body, add_axiom_print=True)
        assert "import Mathlib" in final
        assert "trivial_t" in final
        assert "nlinarith" in final
        assert "#print axioms trivial_t" in final

    def test_build_canonical_strips_leading_assign(self):
        body = ":= by positivity"
        final = build_canonical_submission(self.CANON, body, add_axiom_print=False)
        # Should not have ':= :=' in the output
        assert ":= :=" not in final
        assert "positivity" in final


# =============================================================================
# compile_loop mode: validate_mode and _VALID_MODES
# =============================================================================

class TestCompileLoopMode:
    """Tests for the compile_loop ablation mode (Change 5)."""

    def test_validate_mode_compile_loop(self):
        assert validate_mode("compile_loop") == "compile_loop"

    def test_compile_loop_in_valid_modes(self):
        from local_goedel.config import _VALID_MODES
        assert "compile_loop" in _VALID_MODES


# =============================================================================
# ToolRegistry.schemas with allowed filter
# =============================================================================

class TestToolRegistrySchemas:
    """Test the optional allowed-tools filter on ToolRegistry.schemas."""

    def _make_registry(self):
        from local_goedel.tools.base import ToolRegistry
        from local_goedel.tools.lean_compile import LeanCompileTool
        from local_goedel.tools.mathlib_search import MathlibSearchTool

        reg = ToolRegistry()
        reg.register(LeanCompileTool())
        reg.register(MathlibSearchTool())
        return reg

    def test_schemas_no_filter_returns_both(self):
        reg = self._make_registry()
        schemas = reg.schemas()
        names = {s["function"]["name"] for s in schemas}
        assert names == {"lean_compile", "mathlib_search"}

    def test_schemas_filter_lean_compile_only(self):
        reg = self._make_registry()
        schemas = reg.schemas({"lean_compile"})
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "lean_compile"

    def test_schemas_filter_mathlib_search_only(self):
        reg = self._make_registry()
        schemas = reg.schemas({"mathlib_search"})
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "mathlib_search"

    def test_schemas_empty_filter_returns_none(self):
        reg = self._make_registry()
        schemas = reg.schemas(set())
        assert schemas == []

    def test_schemas_none_filter_returns_all(self):
        """schemas(None) is the same as schemas() — all tools returned."""
        reg = self._make_registry()
        assert len(reg.schemas(None)) == 2


# =============================================================================
# compile_loop pipeline dispatch: _run_tool_loop called with allowed_tools={"lean_compile"}
# =============================================================================

class TestCompileLoopDispatch:
    """Verify that Pipeline.run('compile_loop') routes to _run_tool_loop with
    allowed_tools={"lean_compile"} and that mathlib_search is never invoked."""

    def _make_pipeline(self, mode: str = "compile_loop"):
        from local_goedel.orchestrator.pipeline import Pipeline

        settings = Settings(
            api_key="test-key",
            mode=mode,
            runs_dir=str(Path("/tmp/test_runs")),
        )
        with (
            patch("local_goedel.orchestrator.pipeline.LeanClient"),
            patch("local_goedel.orchestrator.pipeline.MathlibSearchClient"),
            patch("local_goedel.orchestrator.pipeline.LLMClient"),
            patch("local_goedel.orchestrator.pipeline.Prover"),
            patch("local_goedel.orchestrator.pipeline.Synthesizer"),
            patch("local_goedel.orchestrator.pipeline.BlueprintGenerator"),
            patch("local_goedel.orchestrator.pipeline.BlueprintRefiner"),
        ):
            return Pipeline(settings)

    def test_dispatch_compile_loop(self, tmp_path):
        """compile_loop calls _run_tool_loop; full/oneshot are never called."""
        pipeline = self._make_pipeline("compile_loop")

        lean_file = tmp_path / "t.lean"
        lean_file.write_text(
            "import Mathlib\ntheorem t (a b : Int) : a + b = b + a := by sorry\n",
            encoding="utf-8",
        )

        stub_outcome = RunOutcome(success=True, reason="stubbed")
        calls: dict = {"tool_loop": [], "full": 0, "oneshot": 0}

        def fake_tool_loop(*a, **kw):
            calls["tool_loop"].append(kw)
            return stub_outcome

        def fake_full(*a, **kw):
            calls["full"] += 1
            return stub_outcome

        def fake_oneshot(*a, **kw):
            calls["oneshot"] += 1
            return stub_outcome

        pipeline._run_full = fake_full
        pipeline._run_tool_loop = fake_tool_loop
        pipeline._run_oneshot = fake_oneshot

        with (
            patch("local_goedel.orchestrator.pipeline.make_run_id", return_value="t_000"),
            patch("local_goedel.orchestrator.pipeline.ArtifactWriter") as mock_aw,
        ):
            mock_aw.return_value.log_path.return_value = tmp_path / "run.log"
            mock_aw.return_value.save_config = MagicMock()
            mock_aw.return_value.save_theorem = MagicMock()
            mock_aw.return_value.save_outcome = MagicMock()

            outcome = pipeline.run(theorem_file_path=lean_file, mode="compile_loop")

        # _run_tool_loop was called exactly once
        assert len(calls["tool_loop"]) == 1
        # allowed_tools={"lean_compile"} was forwarded
        assert calls["tool_loop"][0].get("allowed_tools") == {"lean_compile"}
        # other branches untouched
        assert calls["full"] == 0
        assert calls["oneshot"] == 0
        # outcome.mode is set to compile_loop
        assert outcome.mode == "compile_loop"

    def test_compile_loop_tool_loop_gets_lean_compile_only_schemas(self):
        """ToolRegistry.schemas({"lean_compile"}) for compile_loop mode hides mathlib_search."""
        from local_goedel.tools.base import ToolRegistry
        from local_goedel.tools.lean_compile import LeanCompileTool
        from local_goedel.tools.mathlib_search import MathlibSearchTool

        reg = ToolRegistry()
        reg.register(LeanCompileTool())
        reg.register(MathlibSearchTool())

        # Simulates what Agent.run() sees in compile_loop mode
        schemas = reg.schemas({"lean_compile"})
        names = [s["function"]["name"] for s in schemas]
        assert names == ["lean_compile"]
        assert "mathlib_search" not in names

    def test_agent_config_allowed_tools_propagated(self):
        """AgentConfig.allowed_tools is forwarded to registry.schemas in Agent.run()."""
        from unittest.mock import call, patch
        from local_goedel.agents.agent import Agent, AgentConfig
        from local_goedel.tools.base import ToolContext, ToolRegistry
        from local_goedel.tools.lean_compile import LeanCompileTool
        from local_goedel.tools.mathlib_search import MathlibSearchTool
        from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus

        reg = ToolRegistry()
        reg.register(LeanCompileTool())
        reg.register(MathlibSearchTool())

        config = AgentConfig(
            max_turns=1,
            max_tool_calls=1,
            system_prompt="",
            allowed_tools={"lean_compile"},
        )

        # Mock LLM to return a terminal (no-tool-call) response immediately
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "done"
        mock_response.tool_calls = []
        mock_llm.chat.return_value = mock_response

        dummy_node = BlueprintNode(
            id="n1",
            lean_name="t",
            signature=": True",
            kind=NodeKind.TARGET,
            status=NodeStatus.PENDING,
        )
        ctx = ToolContext(
            lean_client=MagicMock(),
            mathlib_client=MagicMock(),
            target_node=dummy_node,
            parent_nodes=[],
            settings=None,
            logger=MagicMock(),
        )

        agent = Agent(llm_client=mock_llm, registry=reg, config=config)
        agent.run("prove something", ctx)

        # Inspect the tools= kwarg passed to llm.chat
        assert mock_llm.chat.called
        _, chat_kwargs = mock_llm.chat.call_args
        tools_passed = chat_kwargs.get("tools") or []
        tool_names = [t["function"]["name"] for t in tools_passed]
        assert tool_names == ["lean_compile"]
        assert "mathlib_search" not in tool_names
