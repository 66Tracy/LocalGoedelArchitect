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


def _parse_response_dict(response: dict[str, Any]) -> tuple[list[LeanMessage], int, list[Any]]:
    """Parse a raw Lean REPL response dict into (messages, env, sorries).

    Shared by both from_payload and from_kimina so the parsing logic is not duplicated.
    """
    raw_messages = response.get("messages", [])
    messages: list[LeanMessage] = []
    for msg in raw_messages:
        messages.append(
            LeanMessage(
                severity=msg.get("severity", "info"),
                data=msg.get("data", ""),
                pos=msg.get("pos", {}),
                end_pos=msg.get("endPos", {}),
            )
        )
    env = response.get("env", 0)
    sorries = response.get("sorries", [])
    return messages, env, sorries


@dataclass
class CheckResult:
    snippet_id: str
    messages: list[LeanMessage] = field(default_factory=list)
    env: int = 0
    sorries: list[Any] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    status: str = ""  # one of: valid | lean_error | sorry | timeout_error | "" (unknown/HTTP)

    @property
    def errors(self) -> list[LeanMessage]:
        return [m for m in self.messages if m.severity == "error"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def mentions_sorry(self) -> bool:
        """Return True if any warning/error message mentions 'sorry', or status=='sorry'."""
        if self.status == "sorry":
            return True
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

        messages, env, sorries = _parse_response_dict(response)

        return cls(
            snippet_id=snippet_id,
            messages=messages,
            env=env,
            sorries=sorries,
            raw=response,
            status="",  # HTTP path: status unknown
        )

    @classmethod
    def from_kimina(cls, repl_response: Any, snippet_id: str = "") -> "CheckResult":
        """Parse from a single kimina ReplResponse object (res.results[i]).

        repl_response.analyze().status.value -> str in {valid, lean_error, sorry, timeout_error}
        repl_response.response                -> raw dict (same shape as from_payload's response)
        """
        status = repl_response.analyze().status.value
        response: dict[str, Any] = repl_response.response or {}
        messages, env, sorries = _parse_response_dict(response)

        return cls(
            snippet_id=snippet_id,
            messages=messages,
            env=env,
            sorries=sorries,
            raw=response,
            status=status,
        )
