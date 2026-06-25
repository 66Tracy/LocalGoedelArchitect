"""Unit tests for the telemetry accumulator."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from local_goedel.telemetry import (
    RunTelemetry,
    get_telemetry,
    telemetry_scope,
)


# ---------------------------------------------------------------------------
# RunTelemetry.add_usage
# ---------------------------------------------------------------------------

class TestAddUsage:
    def _make_usage(self, prompt=10, completion=20, reasoning=5):
        details = SimpleNamespace(reasoning_tokens=reasoning)
        return SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            completion_tokens_details=details,
        )

    def test_basic_accumulation(self):
        tel = RunTelemetry()
        usage = self._make_usage(prompt=10, completion=20, reasoning=5)
        tel.add_usage(usage)
        assert tel.prompt_tokens == 10
        assert tel.completion_tokens == 20
        assert tel.reasoning_tokens == 5

    def test_total_tokens_excludes_reasoning(self):
        tel = RunTelemetry()
        usage = self._make_usage(prompt=100, completion=200, reasoning=50)
        tel.add_usage(usage)
        # reasoning is a subset of completion, must NOT be double-counted
        assert tel.total_tokens == 300

    def test_none_is_noop(self):
        tel = RunTelemetry()
        tel.add_usage(None)
        assert tel.prompt_tokens == 0
        assert tel.completion_tokens == 0
        assert tel.reasoning_tokens == 0

    def test_no_completion_tokens_details(self):
        """usage without completion_tokens_details must not crash."""
        usage = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=10,
            total_tokens=15,
            completion_tokens_details=None,
        )
        tel = RunTelemetry()
        tel.add_usage(usage)
        assert tel.prompt_tokens == 5
        assert tel.completion_tokens == 10
        assert tel.reasoning_tokens == 0

    def test_missing_completion_tokens_details_attr(self):
        """usage object without completion_tokens_details attribute at all."""
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=7, total_tokens=10)
        tel = RunTelemetry()
        tel.add_usage(usage)
        assert tel.reasoning_tokens == 0

    def test_accumulates_across_calls(self):
        tel = RunTelemetry()
        tel.add_usage(self._make_usage(prompt=10, completion=20, reasoning=3))
        tel.add_usage(self._make_usage(prompt=5, completion=15, reasoning=2))
        assert tel.prompt_tokens == 15
        assert tel.completion_tokens == 35
        assert tel.reasoning_tokens == 5


# ---------------------------------------------------------------------------
# RunTelemetry.add_tool_call
# ---------------------------------------------------------------------------

class TestAddToolCall:
    def test_count_single_tool(self):
        tel = RunTelemetry()
        tel.add_tool_call("lean_compile")
        assert tel.tool_calls["lean_compile"] == 1

    def test_count_multiple_calls_same_tool(self):
        tel = RunTelemetry()
        tel.add_tool_call("lean_compile")
        tel.add_tool_call("lean_compile")
        tel.add_tool_call("lean_compile")
        assert tel.tool_calls["lean_compile"] == 3

    def test_count_different_tools(self):
        tel = RunTelemetry()
        tel.add_tool_call("lean_compile")
        tel.add_tool_call("mathlib_search")
        assert tel.tool_calls["lean_compile"] == 1
        assert tel.tool_calls["mathlib_search"] == 1


# ---------------------------------------------------------------------------
# RunTelemetry.merge
# ---------------------------------------------------------------------------

class TestMerge:
    def test_merge_sums_tokens(self):
        a = RunTelemetry(prompt_tokens=10, completion_tokens=20, reasoning_tokens=5)
        b = RunTelemetry(prompt_tokens=3, completion_tokens=7, reasoning_tokens=2)
        a.merge(b)
        assert a.prompt_tokens == 13
        assert a.completion_tokens == 27
        assert a.reasoning_tokens == 7

    def test_merge_sums_tool_calls(self):
        a = RunTelemetry()
        a.tool_calls = {"lean_compile": 2, "mathlib_search": 1}
        b = RunTelemetry()
        b.tool_calls = {"lean_compile": 3, "other_tool": 4}
        a.merge(b)
        assert a.tool_calls["lean_compile"] == 5
        assert a.tool_calls["mathlib_search"] == 1
        assert a.tool_calls["other_tool"] == 4

    def test_merge_empty_into_empty(self):
        a = RunTelemetry()
        b = RunTelemetry()
        a.merge(b)
        assert a.total_tokens == 0
        assert a.tool_calls == {}


# ---------------------------------------------------------------------------
# telemetry_scope and get_telemetry
# ---------------------------------------------------------------------------

class TestTelemetryScope:
    def test_get_telemetry_returns_none_outside_scope(self):
        assert get_telemetry() is None

    def test_get_telemetry_returns_tel_inside_scope(self):
        tel = RunTelemetry()
        with telemetry_scope(tel) as t:
            assert t is tel
            assert get_telemetry() is tel

    def test_get_telemetry_returns_none_after_scope(self):
        tel = RunTelemetry()
        with telemetry_scope(tel):
            pass
        assert get_telemetry() is None

    def test_scope_resets_on_exception(self):
        tel = RunTelemetry()
        try:
            with telemetry_scope(tel):
                raise ValueError("oops")
        except ValueError:
            pass
        assert get_telemetry() is None

    def test_nested_scopes(self):
        outer = RunTelemetry(prompt_tokens=1)
        inner = RunTelemetry(prompt_tokens=2)
        with telemetry_scope(outer):
            assert get_telemetry() is outer
            with telemetry_scope(inner):
                assert get_telemetry() is inner
            assert get_telemetry() is outer
        assert get_telemetry() is None


# ---------------------------------------------------------------------------
# Integration: ToolRegistry.dispatch hooks telemetry
# ---------------------------------------------------------------------------

class TestToolDispatchHook:
    def test_dispatch_increments_tool_calls(self):
        from local_goedel.tools.base import ToolRegistry, ToolResult, ToolContext
        from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus

        class DummyTool:
            name = "dummy_tool"
            schema: dict = {"description": "dummy", "parameters": {}}

            def run(self, args, ctx):
                return ToolResult(ok=True, content="ok")

        registry = ToolRegistry()
        registry.register(DummyTool())

        node = BlueprintNode(
            id="n1",
            lean_name="foo",
            signature="foo : True",
            kind=NodeKind.LEMMA,
            status=NodeStatus.READY,
        )
        ctx = ToolContext(
            lean_client=None,
            mathlib_client=None,
            target_node=node,
            parent_nodes=[],
            settings=None,
            logger=__import__("logging").getLogger("test"),
        )

        tel = RunTelemetry()
        with telemetry_scope(tel):
            registry.dispatch("dummy_tool", {}, ctx)
            registry.dispatch("dummy_tool", {}, ctx)

        assert tel.tool_calls.get("dummy_tool") == 2

    def test_dispatch_outside_scope_no_error(self):
        """dispatch without an active scope must not raise."""
        from local_goedel.tools.base import ToolRegistry, ToolResult, ToolContext
        from local_goedel.domain.node import BlueprintNode, NodeKind, NodeStatus

        class DummyTool2:
            name = "dummy_tool2"
            schema: dict = {"description": "dummy2", "parameters": {}}

            def run(self, args, ctx):
                return ToolResult(ok=True, content="ok")

        registry = ToolRegistry()
        registry.register(DummyTool2())

        node = BlueprintNode(
            id="n1",
            lean_name="bar",
            signature="bar : True",
            kind=NodeKind.LEMMA,
            status=NodeStatus.READY,
        )
        ctx = ToolContext(
            lean_client=None,
            mathlib_client=None,
            target_node=node,
            parent_nodes=[],
            settings=None,
            logger=__import__("logging").getLogger("test"),
        )
        # No telemetry_scope — must not raise
        result = registry.dispatch("dummy_tool2", {}, ctx)
        assert result.ok is True


# ---------------------------------------------------------------------------
# Integration: add_usage hook fires inside scope
# ---------------------------------------------------------------------------

class TestAddUsageHook:
    def test_add_usage_inside_scope(self):
        """Verify that tel.add_usage works when called directly inside a scope."""
        tel = RunTelemetry()
        usage = SimpleNamespace(
            prompt_tokens=7,
            completion_tokens=13,
            total_tokens=20,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        )
        with telemetry_scope(tel):
            active = get_telemetry()
            assert active is tel
            active.add_usage(usage)

        assert tel.prompt_tokens == 7
        assert tel.completion_tokens == 13
        assert tel.reasoning_tokens == 4
        assert tel.total_tokens == 20
