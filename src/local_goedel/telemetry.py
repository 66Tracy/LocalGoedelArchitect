from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunTelemetry:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        # reasoning_tokens is a subset of completion_tokens, do NOT add it again
        return self.prompt_tokens + self.completion_tokens

    def add_usage(self, usage) -> None:
        """Defensively read token counts from an OpenAI-style usage object."""
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "completion_tokens_details", None)
        self.reasoning_tokens += getattr(details, "reasoning_tokens", 0) or 0

    def add_tool_call(self, name: str) -> None:
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": dict(self.tool_calls),
        }

    def merge(self, other: "RunTelemetry") -> None:
        """Accumulate another RunTelemetry into self."""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        for name, count in other.tool_calls.items():
            self.tool_calls[name] = self.tool_calls.get(name, 0) + count


current_telemetry: ContextVar[Optional[RunTelemetry]] = ContextVar(
    "current_telemetry", default=None
)


def get_telemetry() -> Optional[RunTelemetry]:
    """Return the current RunTelemetry, or None if not bound."""
    return current_telemetry.get()


@contextmanager
def telemetry_scope(tel: RunTelemetry):
    """Context manager that binds tel as the current telemetry and resets on exit."""
    token = current_telemetry.set(tel)
    try:
        yield tel
    finally:
        current_telemetry.reset(token)
