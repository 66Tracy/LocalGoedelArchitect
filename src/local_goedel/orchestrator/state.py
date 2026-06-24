"""Orchestration state models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.lean_check import CheckResult


@dataclass
class IterationRecord:
    iteration: int
    ready_node_ids: list[str]
    results: list[Any]  # list[ProofResult]
    refined: bool = False


@dataclass
class RunOutcome:
    success: bool
    final_code: Optional[str] = None
    check: Optional[CheckResult] = None
    blueprint: Optional[Blueprint] = None
    reason: str = ""
    iterations_used: int = 0


@dataclass
class RunState:
    run_id: str
    theorem_src: str
    problem_name: str
    iterations: list[IterationRecord] = field(default_factory=list)
    outcome: Optional[RunOutcome] = None
