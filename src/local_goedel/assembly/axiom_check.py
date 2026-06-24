"""Axiom checking utilities."""
from __future__ import annotations

from local_goedel.domain.lean_check import CheckResult


def uses_sorry(check: CheckResult, lean_name: str) -> bool:
    """Return True if the theorem uses sorryAx (i.e., uses sorry).

    Looks for an info message like:
      '<lean_name>' depends on axioms: [sorryAx, ...]

    Falls back to check.mentions_sorry() if no such message is found.
    """
    prefix = f"'{lean_name}' depends on axioms"
    for msg in check.messages:
        if msg.severity == "info" and msg.data.startswith(prefix):
            return "sorryAx" in msg.data
    # fallback
    return check.mentions_sorry()
