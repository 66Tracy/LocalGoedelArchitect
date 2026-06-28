"""Unit tests for MathlibSearchTool cap enforcement."""
from unittest.mock import MagicMock

from local_goedel.domain.node import BlueprintNode, NodeKind
from local_goedel.tools.base import ToolContext
from local_goedel.tools.mathlib_search import MathlibSearchTool


def _make_ctx(search_return=None):
    node = BlueprintNode(
        id="t1",
        lean_name="myThm",
        signature="(n : Nat) : n = n",
        kind=NodeKind.TARGET,
    )
    mathlib_client = MagicMock()
    mathlib_client.search.return_value = search_return or []
    return ToolContext(
        lean_client=MagicMock(),
        mathlib_client=mathlib_client,
        target_node=node,
        parent_nodes=[],
        settings=MagicMock(),
        logger=MagicMock(),
    )


def test_limit_capped_at_5():
    """When limit=10 is requested, the client is called with limit=5 (cap enforced)."""
    ctx = _make_ctx()
    tool = MathlibSearchTool()
    tool.run({"query": "x", "limit": 10}, ctx)
    ctx.mathlib_client.search.assert_called_once_with("x", limit=5)


def test_limit_below_cap_passes_through():
    """When limit=3 is requested, the client is called with limit=3 (under cap)."""
    ctx = _make_ctx()
    tool = MathlibSearchTool()
    tool.run({"query": "rings", "limit": 3}, ctx)
    ctx.mathlib_client.search.assert_called_once_with("rings", limit=3)


def test_limit_at_cap_passes_through():
    """When limit=5 is requested, the client is called with limit=5 (exactly at cap)."""
    ctx = _make_ctx()
    tool = MathlibSearchTool()
    tool.run({"query": "groups", "limit": 5}, ctx)
    ctx.mathlib_client.search.assert_called_once_with("groups", limit=5)


def test_limit_floor_at_1():
    """limit=0 is raised to 1 by the floor clamp."""
    ctx = _make_ctx()
    tool = MathlibSearchTool()
    tool.run({"query": "test", "limit": 0}, ctx)
    ctx.mathlib_client.search.assert_called_once_with("test", limit=1)


def test_default_limit_is_5():
    """When limit is omitted, the client is called with limit=5."""
    ctx = _make_ctx()
    tool = MathlibSearchTool()
    tool.run({"query": "natural numbers"}, ctx)
    ctx.mathlib_client.search.assert_called_once_with("natural numbers", limit=5)
