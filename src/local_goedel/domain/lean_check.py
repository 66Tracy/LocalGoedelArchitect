"""Domain models for Lean checker results."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LeanMessage:
    severity: str
    data: str
    pos: dict[str, int] = field(default_factory=dict)
    end_pos: dict[str, int] = field(default_factory=dict)


@dataclass
class CheckResult:
    snippet_id: str
    messages: list[LeanMessage] = field(default_factory=list)
    env: int = 0
    sorries: list[Any] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[LeanMessage]:
        return [m for m in self.messages if m.severity == "error"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def mentions_sorry(self) -> bool:
        """Return True if any warning/error message mentions 'sorry'."""
        for m in self.messages:
            if m.severity in ("warning", "error") and "sorry" in m.data.lower():
                return True
        if self.sorries:
            return True
        return False

    @classmethod
    def from_payload(cls, payload: dict[str, Any], snippet_id: str) -> "CheckResult":
        """Parse from API response payload.

        Expected shape: payload["results"][i]["response"]
        where i is found by matching snippet_id.
        """
        results = payload.get("results", [])
        response: dict[str, Any] = {}

        for item in results:
            if item.get("id") == snippet_id:
                response = item.get("response", {})
                break

        if not response and results:
            # fallback: take first result
            response = results[0].get("response", {})

        raw_messages = response.get("messages", [])
        messages = []
        for msg in raw_messages:
            messages.append(
                LeanMessage(
                    severity=msg.get("severity", "info"),
                    data=msg.get("data", ""),
                    pos=msg.get("pos", {}),
                    end_pos=msg.get("endPos", {}),
                )
            )

        return cls(
            snippet_id=snippet_id,
            messages=messages,
            env=response.get("env", 0),
            sorries=response.get("sorries", []),
            raw=response,
        )
