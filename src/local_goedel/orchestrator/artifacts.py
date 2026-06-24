"""Artifact writing utilities."""
from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, Optional

from local_goedel.domain.blueprint import Blueprint
from local_goedel.domain.lean_check import CheckResult
from local_goedel.orchestrator.state import RunOutcome


def _default_serializer(obj: Any) -> Any:
    """JSON serializer for non-standard types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=_default_serializer, ensure_ascii=False)


def _save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_run_id(problem_name: str, runs_dir: Path) -> str:
    """Create a unique run_id based on problem name + counter."""
    base = problem_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    # Find next available counter
    i = 0
    while (runs_dir / f"{base}_{i:03d}").exists():
        i += 1
    return f"{base}_{i:03d}"


class ArtifactWriter:
    """Writes run artifacts to runs/<run_id>/."""

    def __init__(self, run_id: str, runs_dir: Path, logger: Optional[logging.Logger] = None) -> None:
        self.run_id = run_id
        self.run_dir = runs_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logger or logging.getLogger(__name__)

    def save_config(self, settings: Any) -> None:
        """Save config (without API key) to config.json."""
        if dataclasses.is_dataclass(settings):
            d = dataclasses.asdict(settings)
        else:
            d = dict(vars(settings))
        # Never log the API key
        d.pop("api_key", None)
        d.pop("leandex_api_key", None)
        # Convert Path objects to strings
        for k, v in d.items():
            if isinstance(v, Path):
                d[k] = str(v)
        _save_json(self.run_dir / "config.json", d)

    def save_theorem(self, theorem_src: str) -> None:
        """Save original theorem file."""
        _save_text(self.run_dir / "theorem.lean", theorem_src)

    def save_blueprint(self, blueprint: Blueprint, iteration: int, suffix: str = "") -> None:
        """Save blueprint as blueprint.iter{NN}[.suffix].json."""
        fname = f"blueprint.iter{iteration:02d}"
        if suffix:
            fname += f".{suffix}"
        fname += ".json"
        _save_json(self.run_dir / fname, blueprint.to_json())

    def save_node_result(
        self,
        iteration: int,
        node_id: str,
        result: Any,
        transcript: Optional[list[dict]] = None,
    ) -> None:
        """Save node proof result and transcript."""
        iter_dir = self.run_dir / f"iter{iteration:02d}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        _save_json(iter_dir / f"node_{node_id}.result.json", result)
        if transcript:
            _save_json(iter_dir / f"node_{node_id}.transcript.json", transcript)

    def save_final(self, code: str, check: Optional[CheckResult] = None) -> None:
        """Save final.lean and final.check.json."""
        _save_text(self.run_dir / "final.lean", code)
        if check is not None:
            check_data = {
                "snippet_id": check.snippet_id,
                "has_errors": check.has_errors,
                "errors": [
                    {"severity": m.severity, "data": m.data, "pos": m.pos}
                    for m in check.errors
                ],
                "sorries": check.sorries,
            }
            _save_json(self.run_dir / "final.check.json", check_data)

    def save_outcome(self, outcome: RunOutcome) -> None:
        """Save outcome.json."""
        d = {
            "success": outcome.success,
            "reason": outcome.reason,
            "iterations_used": outcome.iterations_used,
        }
        _save_json(self.run_dir / "outcome.json", d)

    def log_path(self) -> Path:
        """Return path to run.log."""
        return self.run_dir / "run.log"
