"""Tests for the agent loop."""
import json
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from local_goedel.agents.agent import Agent, AgentConfig, AgentRun
from local_goedel.domain.node import BlueprintNode, NodeKind
from local_goedel.tools.base import ToolContext, ToolRegistry, ToolResult


class FakeToolCall:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.role = "assistant"


class FakeLLMClient:
    """Returns tool_call first, then a final message."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return FakeMessage(content="Done.")


class EchoTool:
    name = "echo"
    schema = {
        "description": "Echo a message",
        "parameters": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    }

    def run(self, args, ctx):
        return ToolResult(ok=True, content=f"echo: {args.get('msg', '')}")


def _make_ctx():
    node = BlueprintNode(
        id="t1",
        lean_name="myThm",
        signature="(n : Nat) : n = n",
        kind=NodeKind.TARGET,
    )
    return ToolContext(
        lean_client=None,
        mathlib_client=None,
        target_node=node,
        parent_nodes=[],
        settings=MagicMock(),
        logger=MagicMock(),
    )


def test_tool_dispatched_and_completed():
    """Tool is dispatched then agent completes."""
    tool_call = FakeToolCall("tc1", "echo", {"msg": "hello"})
    llm = FakeLLMClient([
        FakeMessage(tool_calls=[tool_call]),
        FakeMessage(content="All done!"),
    ])

    registry = ToolRegistry()
    registry.register(EchoTool())

    config = AgentConfig(max_turns=5, max_tool_calls=10)
    agent = Agent(llm, registry, config)

    run = agent.run("Do something", _make_ctx())

    assert run.stop_reason == "completed"
    assert run.tool_call_count == 1


def test_max_tool_calls_enforced():
    """Budget exceeded message injected when max_tool_calls hit."""
    tool_call1 = FakeToolCall("tc1", "echo", {"msg": "first"})
    tool_call2 = FakeToolCall("tc2", "echo", {"msg": "second"})
    # LLM tries to make 2 calls when budget is 1
    llm = FakeLLMClient([
        FakeMessage(tool_calls=[tool_call1, tool_call2]),
        FakeMessage(content="Wrapping up."),
    ])

    registry = ToolRegistry()
    registry.register(EchoTool())

    config = AgentConfig(max_turns=5, max_tool_calls=1)
    agent = Agent(llm, registry, config)

    run = agent.run("Use tools", _make_ctx())

    # Should have processed only 1 real tool call, 2nd got budget message
    assert run.tool_call_count == 1
    # Look for budget exceeded message in messages
    budget_msgs = [
        m for m in run.messages
        if m.get("role") == "tool" and "BUDGET EXCEEDED" in m.get("content", "")
    ]
    assert len(budget_msgs) >= 1


def test_no_tool_calls_completes_immediately():
    """Agent with no tool calls completes immediately."""
    llm = FakeLLMClient([FakeMessage(content="Here is the answer.")])
    registry = ToolRegistry()
    config = AgentConfig(max_turns=5, max_tool_calls=10)
    agent = Agent(llm, registry, config)

    run = agent.run("Tell me something", _make_ctx())
    assert run.stop_reason == "completed"
    assert run.tool_call_count == 0
