"""Generic agent loop."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from local_goedel.tools.base import ToolContext, ToolRegistry


@dataclass
class AgentConfig:
    max_turns: int = 60
    max_tool_calls: int = 40
    system_prompt: str = ""
    allowed_tools: Optional[set[str]] = None


@dataclass
class AgentRun:
    stop_reason: str  # "completed" | "max_turns" | "max_tool_calls" | "error"
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_call_count: int = 0
    last_assistant_content: str = ""
    error: Optional[str] = None


class Agent:
    def __init__(
        self,
        llm_client: Any,
        registry: ToolRegistry,
        config: AgentConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._llm = llm_client
        self._registry = registry
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

    def run(self, user_prompt: str, ctx: ToolContext) -> AgentRun:
        """Run the agent loop."""
        messages: list[dict[str, Any]] = []

        if self._config.system_prompt:
            messages.append({"role": "system", "content": self._config.system_prompt})

        messages.append({"role": "user", "content": user_prompt})

        tools = self._registry.schemas(self._config.allowed_tools)
        tool_call_count = 0
        budget_exceeded = False

        for turn in range(self._config.max_turns):
            self._logger.info("Agent turn %d/%d", turn + 1, self._config.max_turns)

            try:
                assistant_msg = self._llm.chat(
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else "none",
                )
            except Exception as e:
                self._logger.error("LLM error: %s", e)
                return AgentRun(
                    stop_reason="error",
                    messages=messages,
                    tool_call_count=tool_call_count,
                    error=str(e),
                )

            # Convert assistant message to dict
            assistant_dict = self._message_to_dict(assistant_msg)
            messages.append(assistant_dict)

            last_content = ""
            if isinstance(assistant_msg.content, str):
                last_content = assistant_msg.content or ""
            elif isinstance(assistant_msg.content, list):
                text_parts = [
                    p.text if hasattr(p, "text") else str(p)
                    for p in assistant_msg.content
                    if hasattr(p, "type") and getattr(p, "type") == "text"
                ]
                last_content = " ".join(text_parts)

            # Check if there are tool calls
            tool_calls = getattr(assistant_msg, "tool_calls", None) or []
            if not tool_calls:
                # No tool calls - agent is done
                self._logger.info("Agent completed (no tool calls)")
                return AgentRun(
                    stop_reason="completed",
                    messages=messages,
                    tool_call_count=tool_call_count,
                    last_assistant_content=last_content,
                )

            # Process tool calls
            tool_results: list[dict[str, Any]] = []
            for tc in tool_calls:
                if budget_exceeded:
                    # Inject budget-exceeded message
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            "BUDGET EXCEEDED: You have used all available tool calls. "
                            "Please provide your best proof attempt now without calling more tools."
                        ),
                    })
                    continue

                if tool_call_count >= self._config.max_tool_calls:
                    budget_exceeded = True
                    self._logger.warning(
                        "Tool call budget exceeded (%d)", self._config.max_tool_calls
                    )
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            "BUDGET EXCEEDED: You have used all available tool calls. "
                            "Please provide your best proof attempt now without calling more tools."
                        ),
                    })
                    continue

                tool_call_count += 1
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                    self._logger.warning("Failed to parse tool args: %s", tc.function.arguments)

                self._logger.info("Dispatching tool %r (call %d)", fn_name, tool_call_count)
                result = self._registry.dispatch(fn_name, fn_args, ctx)

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result.content,
                })

            messages.extend(tool_results)

            if budget_exceeded:
                # Let the agent respond once more then stop
                continue

        self._logger.warning("Agent reached max turns (%d)", self._config.max_turns)
        return AgentRun(
            stop_reason="max_turns",
            messages=messages,
            tool_call_count=tool_call_count,
        )

    def _message_to_dict(self, msg: Any) -> dict[str, Any]:
        """Convert an OpenAI message object to a plain dict."""
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
