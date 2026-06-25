"""Unit tests for the benchmark runner."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from local_goedel.benchmark.runner import (
    load_records,
    map_difficulty,
    run_one,
    run_benchmark,
)
from local_goedel.clients.lean_client import set_lean_concurrency
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

    def test_limit(self, tmp_path):
        records = [_make_record(str(i)) for i in range(10)]
        jsonl = tmp_path / "bench.jsonl"
        _write_jsonl(jsonl, records)
        loaded = load_records(jsonl, limit=3)
        assert len(loaded) == 3

    def test_difficulty_filter_easy(self, tmp_path):
        records = [
            _make_record("easy1", difficulty="K12_primary"),
            _make_record("hard1", difficulty="competition_hard"),
            _make_record("easy2", difficulty="undergraduate"),
        ]
        jsonl = tmp_path / "bench.jsonl"
        _write_jsonl(jsonl, records)
        loaded = load_records(jsonl, difficulty_filter="easy")
        assert len(loaded) == 2
        assert all(r["id"].startswith("easy") for r in loaded)

    def test_difficulty_filter_hard(self, tmp_path):
        records = [
            _make_record("easy1", difficulty="K12_primary"),
            _make_record("hard1", difficulty="competition_hard"),
            _make_record("hard2", difficulty="olympiad_hard"),
        ]
        jsonl = tmp_path / "bench.jsonl"
        _write_jsonl(jsonl, records)
        loaded = load_records(jsonl, difficulty_filter="hard")
        assert len(loaded) == 2

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
# Tests: map_difficulty
# ---------------------------------------------------------------------------

class TestMapDifficulty:
    @pytest.mark.parametrize("difficulty,expected", [
        ("K12_primary", "easy"),
        ("undergraduate", "easy"),
        ("competition_easy", "easy"),
        ("", "easy"),
        ("unknown_category", "easy"),
        ("competition_hard", "hard"),
        ("olympiad_easy", "hard"),   # contains "olympiad"
        ("some_hard_thing", "hard"), # contains "hard"
    ])
    def test_difficulty_mapping(self, difficulty, expected):
        record = _make_record(difficulty=difficulty)
        assert map_difficulty(record) == expected

    def test_putnam_id_is_hard(self):
        record = _make_record(id_="putnam_2025_a1", difficulty="")
        assert map_difficulty(record) == "hard"

    def test_putnam_id_with_easy_difficulty(self):
        # id takes precedence
        record = _make_record(id_="putnam_2025_b2", difficulty="K12_primary")
        assert map_difficulty(record) == "hard"

    def test_empty_difficulty(self):
        record = {"id": "some_id", "difficulty": None, "lean4_code": ""}
        assert map_difficulty(record) == "easy"

    def test_missing_difficulty_key(self):
        record = {"id": "some_id", "lean4_code": ""}
        assert map_difficulty(record) == "easy"


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

    def test_success_result(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = _make_record()

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=True, reason="ok", iterations_used=1
            )
            result = run_one(record, settings, mode="full", difficulty_override=None)

        assert result["success"] is True
        assert result["id"] == "test_001"
        assert result["reason"] == "ok"
        assert result["elapsed_s"] >= 0.0

    def test_failure_result(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = _make_record()

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=False, reason="Failed", iterations_used=3
            )
            result = run_one(record, settings, mode="full", difficulty_override=None)

        assert result["success"] is False
        assert "Failed" in result["reason"]

    def test_exception_becomes_failure_dict(self, tmp_path):
        """Pipeline crash must NOT propagate — run_one wraps it."""
        settings = _make_settings(tmp_path)
        record = _make_record()

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.side_effect = RuntimeError("Server exploded")
            result = run_one(record, settings, mode="full", difficulty_override=None)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "Server exploded" in result["reason"]

    def test_difficulty_override(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = _make_record(difficulty="K12_primary")  # maps to easy

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=False, reason="n/a", iterations_used=0
            )
            result = run_one(record, settings, mode="full", difficulty_override="hard")

        assert result["difficulty"] == "hard"

    def test_auto_difficulty(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = _make_record(difficulty="competition_hard")

        with patch("local_goedel.benchmark.runner.Pipeline") as MockPipeline:
            MockPipeline.return_value.run.return_value = RunOutcome(
                success=False, reason="n/a", iterations_used=0
            )
            result = run_one(record, settings, mode="full", difficulty_override=None)

        assert result["difficulty"] == "hard"

    def test_empty_lean4_code(self, tmp_path):
        settings = _make_settings(tmp_path)
        record = {"id": "bad_rec", "lean4_code": "", "difficulty": "K12_primary"}
        result = run_one(record, settings, mode="full", difficulty_override=None)
        assert result["success"] is False
        assert "Empty" in result["reason"]


# ---------------------------------------------------------------------------
# Tests: run_benchmark (stubbed pipeline)
# ---------------------------------------------------------------------------

class TestRunBenchmark:
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
                lean_concurrency=1,
                out_dir=tmp_path / "out",
            )

        assert agg["total"] == 3
        assert agg["solved"] == 2
        assert (tmp_path / "out" / "summary.jsonl").exists()
        assert (tmp_path / "out" / "summary.json").exists()

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
                lean_concurrency=1,
                out_dir=tmp_path / "out2",
            )

        lines = (tmp_path / "out2" / "summary.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4

    def test_by_difficulty_breakdown(self, tmp_path):
        settings = _make_settings(tmp_path)
        records = [
            _make_record("e1", difficulty="K12_primary"),
            _make_record("h1", difficulty="competition_hard"),
        ]
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
                lean_concurrency=1,
                out_dir=tmp_path / "out3",
            )

        assert "easy" in agg["by_difficulty"]
        assert "hard" in agg["by_difficulty"]
        assert agg["by_difficulty"]["easy"]["total"] == 1
        assert agg["by_difficulty"]["hard"]["total"] == 1

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
                lean_concurrency=1,
                out_dir=tmp_path / "out4",
            )

        failed_ids = [f["id"] for f in agg["failed"]]
        assert "fail1" in failed_ids
        assert "pass1" not in failed_ids


# ---------------------------------------------------------------------------
# Tests: set_lean_concurrency / semaphore
# ---------------------------------------------------------------------------

class TestLeanConcurrency:
    def test_set_lean_concurrency_bounds(self):
        """set_lean_concurrency(n) limits simultaneous entries to n."""
        set_lean_concurrency(2)

        from local_goedel.clients.lean_client import _lean_semaphore

        results = []
        active = {"count": 0}
        lock = threading.Lock()
        peak = {"count": 0}

        def worker():
            _lean_semaphore.acquire()
            try:
                with lock:
                    active["count"] += 1
                    if active["count"] > peak["count"]:
                        peak["count"] = active["count"]
                time.sleep(0.02)
                with lock:
                    active["count"] -= 1
            finally:
                _lean_semaphore.release()
            results.append(1)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        assert peak["count"] <= 2

        # Reset to large value so other tests are not affected
        set_lean_concurrency(1024)

    def test_set_lean_concurrency_invalid(self):
        with pytest.raises(ValueError):
            set_lean_concurrency(0)


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
