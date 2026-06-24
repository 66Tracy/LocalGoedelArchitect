"""Result domain models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from local_goedel.domain.lean_check import CheckResult


class DiagnosisKind(str, Enum):
    SUCCESS = "success"
    COMPILE_ERROR = "compile_error"
    SORRY_USED = "sorry_used"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
    # Phase 2 kinds
    STATEMENT_WRONG = "statement_wrong"
    PROOF_TOO_HARD = "proof_too_hard"
    NONE = "none"


@dataclass
class ProposedLemma:
    lean_name: str
    signature: str
    nl_statement: str = ""
    proof_sketch: str = ""
    parents: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Diagnosis:
    kind: DiagnosisKind
    message: str = ""
    check_result: Optional[CheckResult] = None
    # Phase 2 extended fields
    analysis: str = ""
    suggested_fix: str = ""
    suggested_helpers: list[ProposedLemma] = field(default_factory=list)


@dataclass
class ProofResult:
    success: bool
    proof_body: Optional[str] = None
    check_result: Optional[CheckResult] = None
    diagnosis: Optional[Diagnosis] = None
    iterations: int = 0
