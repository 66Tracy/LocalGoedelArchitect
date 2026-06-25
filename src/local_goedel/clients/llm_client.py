"""LLM client wrapping OpenAI SDK for DeepSeek."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from local_goedel.logging_utils import get_logger
from local_goedel.telemetry import get_telemetry

if TYPE_CHECKING:
    from local_goedel.config import Settings


class LLMClient:
    def __init__(self, settings: "Settings") -> None:
        from openai import OpenAI
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )
        self._model = settings.model_name
        self._max_retries = settings.llm_max_retries
        self._default_reasoning = settings.reasoning_effort
        self._default_thinking = settings.enable_thinking
        self._logger = get_logger(__name__)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: str = "auto",
        reasoning_effort: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
    ) -> Any:
        """Call the LLM and return the assistant message object."""
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            RateLimitError,
        )

        if reasoning_effort is None:
            reasoning_effort = self._default_reasoning
        if enable_thinking is None:
            enable_thinking = self._default_thinking

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if enable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                self._logger.debug(
                    "LLM call attempt %d model=%s msgs=%d",
                    attempt + 1, self._model, len(messages),
                )
                response = self._client.chat.completions.create(**kwargs)
                usage = response.usage
                if usage:
                    self._logger.info(
                        "LLM usage: prompt=%d completion=%d total=%d",
                        usage.prompt_tokens,
                        usage.completion_tokens,
                        usage.total_tokens,
                    )
                tel = get_telemetry()
                if tel is not None:
                    tel.add_usage(usage)
                message = response.choices[0].message
                reasoning_content = getattr(message, "reasoning_content", None)
                if reasoning_content:
                    self._logger.debug(
                        "LLM thinking: %s", reasoning_content[:500]
                    )
                return message
            except RateLimitError as e:
                last_exc = e
                wait = 2 ** attempt * 5
                self._logger.warning("Rate limit (attempt %d), sleeping %ds", attempt + 1, wait)
                time.sleep(wait)
            except (APIConnectionError, APITimeoutError) as e:
                last_exc = e
                wait = 2 ** attempt * 2
                self._logger.warning("Connection error (attempt %d): %s, sleeping %ds", attempt + 1, e, wait)
                time.sleep(wait)
            except APIStatusError as e:
                last_exc = e
                if e.status_code >= 500:
                    wait = 2 ** attempt * 3
                    self._logger.warning("Server error %d (attempt %d), sleeping %ds", e.status_code, attempt + 1, wait)
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError(f"LLM failed after {self._max_retries} attempts") from last_exc
