"""Blueprint node models."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from local_goedel.domain.lean_check import CheckResult


class NodeKind(str, Enum):
    DEFINITION = "definition"
    LEMMA = "lemma"
    TARGET = "target"


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    PROVING = "proving"
    PROVED = "proved"
    UNPROVED = "unproved"
    BLOCKED = "blocked"


class BlueprintNode(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    kind: NodeKind = NodeKind.LEMMA
    lean_name: str
    # Type fragment AFTER name, WITHOUT ":= by ..."
    signature: str
    # For DEFINITION nodes: verbatim Lean declaration text
    # (e.g. "def my_abs (x : Int) : Int := if x >= 0 then x else -x").
    # If None for a DEFINITION, the assembler falls back to
    # "def <lean_name> <signature>" (which may include ":= body" if the LLM
    # embedded it in `signature`).
    definition_text: Optional[str] = None
    nl_statement: str = ""
    proof_sketch: str = ""
    parents: list[str] = Field(default_factory=list)
    status: NodeStatus = NodeStatus.PENDING
    proof: Optional[str] = None
    proof_result: Optional[Any] = None
    iteration_proved: Optional[int] = None
    attempts: int = 0
    created_in_iteration: int = 0
    derived_from: Optional[str] = None
