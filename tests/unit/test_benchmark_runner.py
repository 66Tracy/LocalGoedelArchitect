"""Unit tests for the benchmark runner."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from local_goedel.benchmark.runner import (
    load_records,
    run_one,
    run_benchmark,
    run_problem_passk,
)
from local_goedel.orchestrator.artifacts import make_run_id
from local_goedel.orchestrator.state import RunOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _make_record(
    id_: str = "test_001",
    difficulty: str = "K12_primary",
    lean4_code: str = "import Mathlib\ntheorem test_001 : 1 + 1 = 2 := by norm_num",
    formal_statement: str | None = None,
    header: str | None = None,
) -> dict:
    return {
        "id": id_,
        "difficulty": difficulty,
        "lean4_code": lean4_code,
        "formal_statement": formal_statement or "theorem test_001 : 1 + 1 = 2 := by sorry",
        "header": header or "import Mathlib",
    }


def _make_settings(tmp_path: Path) -> Any:
    from local_goedel.config import Settings
    return Settings(
        api_key="dummy-key",
        runs_dir=str(tmp_path / "runs"),
    )


# ---------------------------------------------------------------------------
# Tests: load_records
# ---------------------------------------------------------------------------

class TestLoadRecords:
    def test_basic_load(self, tmp_path):
        records = [_make_record("a"), _make_record("b"), _make_record("c")]
        jsonl = tmp_path / "bench.jsonl"
        _write_jsonl(jsonl, records)
        loaded = load_records(jsonl)
        assert len(loaded) == 3
        assert [r["id"] for r in loaded] == ["a", "b", "c"]

    def test_skips_blank_lines(self, tmp_path):
        jsonl = tmp_path / "bench.jsonl"
        jsonl.write_text(
            json.dumps(_make_record("a")) + "\n"
            "\n"
            "  \n"
            + json.dumps(_make_record("b")) + "\n",
            encoding="utf-8",
        )
        loaded = load_records(jsonl)
        assert len(loaded) == 2

    def test_skips_malformed_lines(self, tmp_path):
        jsonl = tmp_path / "bench.jsonl"
        jsonl.write_text(
            json.dumps(_make_record("a")) + "\n"
            "NOT VALID JSON {{{\n"
            + json.dumps(_make_record("b")) + "\n",
            encoding="utf-8",
        )
        loaded = load_records(jsonl)
        assert len(loaded) == 2
        assert [r["id"] for r in loaded] == ["a", "b"]

    def test_trailing_newline(self, tmp_path):
        jsonl = tmp_path / "bench.jsonl"
        jsonl.write_text(
            json.dumps(_make_record("x")) + "\n",
            encoding="utf-8",
        )
        loaded = load_records(jsonl)
        assert len(loaded) == 1


# ---------------------------------------------------------------------------
# Tests: run_one (mocked pipeline)
# ---------------------------------------------------------------------------

class TestRunOne:
    def _stub_pipeline_success(self, *args, **kwargs):
        return RunOutcome(success=True, reason="Proved and verified", iterations_used=1)

    def _stub_pipeline_failure(self, *args, **kwargs):
        return RunOutcome(success=False, reason="All attempts failed", iterations_used=3)

    def _stub_pipeline_raise(self, *args, **kwargs):
        raise RuntimeError("Simulated pipeline crash")

    _TELEMETRY_KEYS = {"prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "tool_calls"}

    def _assert_telemetry_shape(self, result: dict) -> None:
        """Assert that result contains a 'telemetry' key with the expected sub-keys."""
        assert "telemetry" in result, "result missing 'telemetry' key"
        tel = result["telemetry"]
        assert isinstance(tel, dict), "'telemetry' must be a dict"
        assert set(tel.keys()) == self._TELEMETRY_KEYS, (
            f"telemetry keys mismatch: {set(tel.keys())} != {self._TELEMETRY_KEYS}"
        )
        assert isinstance(tel["tool_calls"], dict), "'tool_calls' must be a dict"

    def test_success_result(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = _make_record()

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=True, reason="ok", iterations_used=1
            )
            result = run_one(record, settings, mode="full")

        assert result["success"] is True
        assert result["id"] == "test_001"
        assert result["reason"] == "ok"
        assert result["elapsed_s"] >= 0.0
        self._assert_telemetry_shape(result)

    def test_failure_result(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = _make_record()

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=False, reason="Failed", iterations_used=3
            )
            result = run_one(record, settings, mode="full")

        assert result["success"] is False
        assert "Failed" in result["reason"]
        self._assert_telemetry_shape(result)

    def test_exception_becomes_failure_dict(self, tmp_path):
        """Pipeline crash must NOT propagate — run_one wraps it."""
        settings = _make_settings(tmp_path)
        record = _make_record()

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.side_effect = RuntimeError("Server exploded")
            result = run_one(record, settings, mode="full")

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "Server exploded" in result["reason"]
        self._assert_telemetry_shape(result)

    def test_empty_lean4_code(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = {"id": "bad_rec", "lean4_code": "", "difficulty": "K12_primary"}
        result = run_one(record, settings, mode="full")
        assert result["success"] is False
        assert "Empty" in result["reason"]
        self._assert_telemetry_shape(result)


# ---------------------------------------------------------------------------
# Tests: run_benchmark (stubbed pipeline)
# ---------------------------------------------------------------------------

class TestRunBenchmark:
    _TELEMETRY_KEYS = {"prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "tool_calls"}

    def _assert_telemetry_total_shape(self, agg: dict) -> None:
        """Assert that the aggregate contains 'telemetry_total' with correct sub-keys."""
        assert "telemetry_total" in agg, "aggregate missing 'telemetry_total' key"
        tot = agg["telemetry_total"]
        assert isinstance(tot, dict), "'telemetry_total' must be a dict"
        assert set(tot.keys()) == self._TELEMETRY_KEYS, (
            f"telemetry_total keys mismatch: {set(tot.keys())} != {self._TELEMETRY_KEYS}"
        )
        assert isinstance(tot["tool_calls"], dict), "'tool_calls' in telemetry_total must be a dict"

    def _make_jsonl(self, tmp_path: Path, records: list[dict]) -> Path:
        jsonl = tmp_path / "bench.jsonl"
        _write_jsonl(jsonl, records)
        return jsonl

    def test_aggregate_counting(self, tmp_path):
        settings = _make_settings(tmp_path)
        records = [
            _make_record("r1", difficulty="K12_primary"),
            _make_record("r2", difficulty="K12_primary"),
            _make_record("r3", difficulty="competition_hard"),
        ]
        jsonl = self._make_jsonl(tmp_path, records)

        outcomes = [
            RunOutcome(success=True, reason="ok", iterations_used=1),
            RunOutcome(success=False, reason="fail", iterations_used=2),
            RunOutcome(success=True, reason="ok", iterations_used=1),
        ]
        call_count = {"n": 0}

        def mock_run(**kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            return outcomes[idx % len(outcomes)]

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.side_effect = mock_run
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=tmp_path / "out",
            )

        assert agg["total"] == 3
        assert agg["solved"] == 2
        assert (tmp_path / "out" / "summary.jsonl").exists()
        assert (tmp_path / "out" / "summary.json").exists()
        self._assert_telemetry_total_shape(agg)

    def test_summary_jsonl_written_per_record(self, tmp_path):
        settings = _make_settings(tmp_path)
        records = [_make_record(str(i)) for i in range(4)]
        jsonl = self._make_jsonl(tmp_path, records)

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=False, reason="stubbed", iterations_used=0
            )
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=2,
                out_dir=tmp_path / "out2",
            )

        lines = (tmp_path / "out2" / "summary.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4
        self._assert_telemetry_total_shape(agg)

    def test_failed_ids_in_aggregate(self, tmp_path):
        settings = _make_settings(tmp_path)
        records = [
            _make_record("pass1"),
            _make_record("fail1"),
        ]
        jsonl = self._make_jsonl(tmp_path, records)

        outcomes = [
            RunOutcome(success=True, reason="ok", iterations_used=1),
            RunOutcome(success=False, reason="no proof", iterations_used=2),
        ]

        call_count = {"n": 0}

        def mock_run(**kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            return outcomes[idx % len(outcomes)]

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.side_effect = mock_run
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=tmp_path / "out4",
            )

        failed_ids = [f["id"] for f in agg["failed"]]
        assert "fail1" in failed_ids
        assert "pass1" not in failed_ids
        self._assert_telemetry_total_shape(agg)


# ---------------------------------------------------------------------------
# Tests: thread-safe make_run_id
# ---------------------------------------------------------------------------

class TestThreadSafeMakeRunId:
    def test_unique_ids_concurrent(self, tmp_path):
        """Concurrent calls must produce unique run directories."""
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()

        collected = []
        lock = threading.Lock()

        def worker():
            rid = make_run_id("test_prob", runs_dir)
            with lock:
                collected.append(rid)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(collected) == 10
        assert len(set(collected)) == 10, "Duplicate run_ids detected!"
        for rid in collected:
            assert (runs_dir / rid).exists()

    def test_run_id_format(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        rid = make_run_id("my problem/test", runs_dir)
        # Slashes replaced
        assert "/" not in rid
        # Ends with _NNN counter
        assert rid.endswith("_000")


# ---------------------------------------------------------------------------
# Tests: run_problem_passk
# ---------------------------------------------------------------------------

def _make_run_one_result(
    success: bool,
    reason: str = "",
    prompt_tokens: int = 0,
) -> dict:
    """Build a minimal run_one-style result dict for mocking."""
    from local_goedel.telemetry import RunTelemetry
    tel = RunTelemetry(prompt_tokens=prompt_tokens, completion_tokens=5)
    return {
        "id": "test_001",
        "mode": "full",
        "success": success,
        "reason": reason or ("ok" if success else "failed"),
        "elapsed_s": 0.1,
        "run_dir": None,
        "telemetry": tel.as_dict(),
    }


class TestRunProblemPassK:
    """Tests for run_problem_passk early-stop behaviour."""

    def _record(self) -> dict:
        return _make_record("test_001")

    def _settings(self, tmp_path: Path):
        return _make_settings(tmp_path)

    # ------------------------------------------------------------------
    # k=1 — single attempt, behaves like run_one
    # ------------------------------------------------------------------

    def test_k1_success(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()
        run_one_result = _make_run_one_result(success=True, reason="ok")

        with patch("local_goedel.benchmark.runner.run_one", return_value=run_one_result) as mock_run_one:
            result = run_problem_passk(record, settings, mode="full", k=1)

        assert result["passed"] is True
        assert result["success"] is True   # backward-compat alias
        assert result["attempts_used"] == 1
        assert result["k"] == 1
        mock_run_one.assert_called_once()

    def test_k1_failure(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()
        run_one_result = _make_run_one_result(success=False, reason="no proof")

        with patch("local_goedel.benchmark.runner.run_one", return_value=run_one_result) as mock_run_one:
            result = run_problem_passk(record, settings, mode="full", k=1)

        assert result["passed"] is False
        assert result["attempts_used"] == 1
        mock_run_one.assert_called_once()

    # ------------------------------------------------------------------
    # Early-stop on success at attempt 2 of k=3
    # ------------------------------------------------------------------

    def test_success_on_attempt_2_of_3(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()

        side_effects = [
            _make_run_one_result(success=False, reason="fail1"),
            _make_run_one_result(success=True, reason="ok2"),
        ]

        with patch("local_goedel.benchmark.runner.run_one", side_effect=side_effects) as mock_run_one:
            result = run_problem_passk(record, settings, mode="full", k=3)

        assert result["passed"] is True
        assert result["attempts_used"] == 2
        assert result["reason"] == "ok2"
        # Must have stopped after attempt 2 — not 3
        assert mock_run_one.call_count == 2

    # ------------------------------------------------------------------
    # Never succeeds — k=3 attempts all run
    # ------------------------------------------------------------------

    def test_never_succeeds_k3(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()

        side_effects = [
            _make_run_one_result(success=False, reason="fail1"),
            _make_run_one_result(success=False, reason="fail2"),
            _make_run_one_result(success=False, reason="fail3"),
        ]

        with patch("local_goedel.benchmark.runner.run_one", side_effect=side_effects) as mock_run_one:
            result = run_problem_passk(record, settings, mode="full", k=3)

        assert result["passed"] is False
        assert result["attempts_used"] == 3
        assert result["reason"] == "fail3"   # last attempt's reason
        assert mock_run_one.call_count == 3

    # ------------------------------------------------------------------
    # Success on attempt 1 of k=5 — saves 4 attempts
    # ------------------------------------------------------------------

    def test_success_on_attempt_1_of_5(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()
        run_one_result = _make_run_one_result(success=True, reason="instant")

        with patch("local_goedel.benchmark.runner.run_one", return_value=run_one_result) as mock_run_one:
            result = run_problem_passk(record, settings, mode="full", k=5)

        assert result["passed"] is True
        assert result["attempts_used"] == 1
        # Early-stop: remaining 4 attempts never run
        assert mock_run_one.call_count == 1

    # ------------------------------------------------------------------
    # Telemetry summed across attempts
    # ------------------------------------------------------------------

    def test_telemetry_summed_across_attempts(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()

        # Each attempt reports prompt_tokens=10, completion_tokens=5
        side_effects = [
            _make_run_one_result(success=False, prompt_tokens=10),
            _make_run_one_result(success=False, prompt_tokens=10),
            _make_run_one_result(success=False, prompt_tokens=10),
        ]

        with patch("local_goedel.benchmark.runner.run_one", side_effect=side_effects):
            result = run_problem_passk(record, settings, mode="full", k=3)

        tel = result["telemetry"]
        assert tel["prompt_tokens"] == 30        # 3 × 10
        assert tel["completion_tokens"] == 15    # 3 × 5
        assert tel["total_tokens"] == 45         # prompt + completion

    # ------------------------------------------------------------------
    # Result dict structure
    # ------------------------------------------------------------------

    def test_result_dict_keys(self, tmp_path):
        settings = self._settings(tmp_path)
        record = self._record()
        run_one_result = _make_run_one_result(success=True)

        with patch("local_goedel.benchmark.runner.run_one", return_value=run_one_result):
            result = run_problem_passk(record, settings, mode="full", k=2)

        expected_keys = {"id", "mode", "k", "passed", "success", "attempts_used",
                         "reason", "elapsed_s", "telemetry", "attempts"}
        assert expected_keys.issubset(result.keys()), (
            f"Missing keys: {expected_keys - set(result.keys())}"
        )
        # attempts list has one entry per actual attempt
        assert isinstance(result["attempts"], list)
        assert len(result["attempts"]) == result["attempts_used"]
        # Each attempt entry has the required sub-keys
        for a in result["attempts"]:
            for key in ("attempt", "success", "reason", "elapsed_s", "run_dir", "telemetry"):
                assert key in a, f"attempt entry missing key '{key}'"


# ---------------------------------------------------------------------------
# Tests: run_benchmark with pass@k
# ---------------------------------------------------------------------------

class TestRunBenchmarkPassK:
    """End-to-end tests for run_benchmark aggregate pass@k fields."""

    _TELEMETRY_KEYS = {"prompt_tokens", "completion_tokens", "reasoning_tokens",
                       "total_tokens", "tool_calls"}

    def _make_jsonl(self, tmp_path: Path, records: list[dict]) -> Path:
        jsonl = tmp_path / "bench.jsonl"
        _write_jsonl(jsonl, records)
        return jsonl

    def test_aggregate_has_k_field(self, tmp_path):
        settings = _make_settings(tmp_path)
        records = [_make_record("r1"), _make_record("r2")]
        jsonl = self._make_jsonl(tmp_path, records)

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=True, reason="ok", iterations_used=1
            )
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=tmp_path / "out",
                k=3,
            )

        assert agg["k"] == 3

    def test_aggregate_attempts_distribution_and_total(self, tmp_path):
        """With k=3 and 2 problems both succeeding on first attempt,
        attempts_distribution == {"1": 2} and total_attempts == 2."""
        settings = _make_settings(tmp_path)
        records = [_make_record("r1"), _make_record("r2")]
        jsonl = self._make_jsonl(tmp_path, records)

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=True, reason="ok", iterations_used=1
            )
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=tmp_path / "out_dist",
                k=3,
            )

        assert "attempts_distribution" in agg
        assert isinstance(agg["attempts_distribution"], dict)
        assert "total_attempts" in agg
        # Both problems solved on attempt 1
        assert agg["attempts_distribution"].get("1", 0) == 2
        assert agg["total_attempts"] == 2

    def test_aggregate_solved_counts_problems_not_attempts(self, tmp_path):
        """solved must count PROBLEMS (pass@k), not individual run_one calls."""
        settings = _make_settings(tmp_path)
        # 3 problems: first passes, second fails, third passes
        records = [_make_record("p1"), _make_record("p2"), _make_record("p3")]
        jsonl = self._make_jsonl(tmp_path, records)

        call_count = {"n": 0}
        outcomes_by_id = {
            "p1": RunOutcome(success=True, reason="ok", iterations_used=1),
            "p2": RunOutcome(success=False, reason="fail", iterations_used=1),
            "p3": RunOutcome(success=True, reason="ok", iterations_used=1),
        }
        # pipeline.run is called with keyword args; we need to return per-record outcome.
        # We track which problem is being run by call order (workers=1, serial)
        problem_order = ["p1", "p2", "p3"]

        def mock_run(**kwargs):
            idx = call_count["n"]
            call_count["n"] += 1
            return outcomes_by_id[problem_order[idx % len(problem_order)]]

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.side_effect = mock_run
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=tmp_path / "out_solved",
                k=1,
            )

        assert agg["total"] == 3
        assert agg["solved"] == 2  # p1 and p3 passed

    def test_aggregate_telemetry_total_summed_over_attempts(self, tmp_path):
        """telemetry_total must aggregate across all problems AND their attempts."""
        settings = _make_settings(tmp_path)
        records = [_make_record("r1")]
        jsonl = self._make_jsonl(tmp_path, records)

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=True, reason="ok", iterations_used=1
            )
            agg = run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=tmp_path / "out_tel",
                k=1,
            )

        assert "telemetry_total" in agg
        tot = agg["telemetry_total"]
        assert isinstance(tot, dict)
        assert set(tot.keys()) == self._TELEMETRY_KEYS

    def test_summary_json_written_with_new_keys(self, tmp_path):
        """summary.json on disk must contain k, attempts_distribution, total_attempts."""
        settings = _make_settings(tmp_path)
        records = [_make_record("r1")]
        jsonl = self._make_jsonl(tmp_path, records)
        out_dir = tmp_path / "out_json"

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=False, reason="nope", iterations_used=1
            )
            run_benchmark(
                jsonl_path=jsonl,
                settings=settings,
                mode="full",
                workers=1,
                out_dir=out_dir,
                k=2,
            )

        written = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        assert written["k"] == 2
        assert "attempts_distribution" in written
        assert "total_attempts" in written
        assert "telemetry_total" in written
        assert "solved" in written
