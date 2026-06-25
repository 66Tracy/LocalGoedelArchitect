"""Benchmark runner: load JSONL problems and run them in parallel."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from local_goedel.assembly.canonical import split_canonical
from local_goedel.clients.lean_client import set_lean_concurrency
from local_goedel.logging_utils import get_logger
from local_goedel.orchestrator.pipeline import Pipeline

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Record loading
# ---------------------------------------------------------------------------

def load_records(
    jsonl_path: str | Path,
    limit: Optional[int] = None,
    difficulty_filter: Optional[str] = None,
) -> list[dict]:
    """Load benchmark records from a JSONL file.

    Args:
        jsonl_path:        Path to the JSONL file.
        limit:             If given, return at most this many records.
        difficulty_filter: If given (e.g. "easy" or "hard"), keep only records
                           whose mapped difficulty matches.  Uses map_difficulty().

    Returns:
        List of record dicts (parsed JSON objects).
    """
    records: list[dict] = []
    path = Path(jsonl_path)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # Skip malformed lines
                continue
            if difficulty_filter is not None:
                if map_difficulty(record) != difficulty_filter:
                    continue
            records.append(record)
            if limit is not None and len(records) >= limit:
                break
    return records


# ---------------------------------------------------------------------------
# Difficulty mapping
# ---------------------------------------------------------------------------

def map_difficulty(record: dict) -> str:
    """Map a benchmark record's difficulty string to "easy" or "hard".

    Mapping rules:
    - "competition_hard"              -> "hard"
    - anything containing "olympiad"  -> "hard"
    - anything containing "hard"      -> "hard"
    - id starting with "putnam"       -> "hard"
    - "K12*", "undergraduate", "competition_easy", empty/unknown -> "easy"

    The CLI --difficulty flag overrides this mapping at the call site.
    """
    difficulty = (record.get("difficulty") or "").strip().lower()
    problem_id = (record.get("id") or "").lower()

    if difficulty == "competition_hard":
        return "hard"
    if "olympiad" in difficulty:
        return "hard"
    if "hard" in difficulty:
        return "hard"
    if problem_id.startswith("putnam"):
        return "hard"

    return "easy"


# ---------------------------------------------------------------------------
# Run a single record
# ---------------------------------------------------------------------------

def run_one(
    record: dict,
    settings: object,
    mode: Optional[str],
    difficulty_override: Optional[str],
) -> dict:
    """Run the pipeline on a single benchmark record.

    Args:
        record:              Benchmark record dict (must have "lean4_code").
        settings:            Settings object.
        mode:                Pipeline mode override (or None to use settings).
        difficulty_override: "easy" | "hard" | None.  When None, map_difficulty
                             is used to derive the difficulty from the record.

    Returns:
        Result dict with keys:
            id, mode, difficulty, success, reason, elapsed_s, run_dir
    """
    rec_id = record.get("id") or "unknown"
    start = time.time()

    difficulty = difficulty_override if difficulty_override is not None else map_difficulty(record)

    result: dict = {
        "id": rec_id,
        "mode": mode,
        "difficulty": difficulty,
        "success": False,
        "reason": "",
        "elapsed_s": 0.0,
        "run_dir": None,
    }

    try:
        lean4_code = record.get("lean4_code") or ""
        if not lean4_code.strip():
            result["reason"] = "Empty lean4_code in record"
            result["elapsed_s"] = time.time() - start
            return result

        canon = split_canonical(
            lean4_code,
            record.get("formal_statement"),
            record.get("header"),
        )

        pipeline = Pipeline(settings)
        # Pass a dummy theorem_file_path whose .stem is the record id so the
        # run directory uses the record id as the base name.
        dummy_path = f"{rec_id}.lean"
        outcome = pipeline.run(
            theorem_file_path=dummy_path,
            theorem_src=lean4_code,
            canonical=canon,
            difficulty=difficulty,
            mode=mode,
        )

        elapsed = time.time() - start
        result["success"] = outcome.success
        result["reason"] = outcome.reason
        result["elapsed_s"] = elapsed

        # Try to find the run dir that was just created.
        # pipeline.run uses the stem of dummy_path (= rec_id with sanitization)
        # as the base for make_run_id.
        runs_dir = Path(settings.runs_dir)
        if runs_dir.exists():
            base = rec_id.replace(" ", "_").replace("/", "_").replace("\\", "_")
            matching = sorted(
                [d for d in runs_dir.iterdir() if d.name.startswith(base + "_") and d.is_dir()],
                key=lambda d: d.name,
            )
            if matching:
                result["run_dir"] = str(matching[-1])

    except Exception as exc:
        elapsed = time.time() - start
        result["success"] = False
        result["reason"] = f"Exception: {exc}"
        result["elapsed_s"] = elapsed
        logger.error("run_one(%s) raised: %s", rec_id, exc, exc_info=True)

    return result


# ---------------------------------------------------------------------------
# Run the full benchmark
# ---------------------------------------------------------------------------

def run_benchmark(
    jsonl_path: str | Path,
    settings: object,
    mode: Optional[str],
    workers: int = 2,
    lean_concurrency: int = 2,
    limit: Optional[int] = None,
    difficulty_override: Optional[str] = None,
    out_dir: Optional[str | Path] = None,
) -> dict:
    """Run the benchmark over a JSONL file in parallel.

    Args:
        jsonl_path:          Path to the JSONL file.
        settings:            Settings object.
        mode:                Pipeline mode override.
        workers:             ThreadPoolExecutor worker count.
        lean_concurrency:    Max simultaneous Lean server calls.
        limit:               If given, process at most this many records.
        difficulty_override: "easy" | "hard" | None (auto).
        out_dir:             Output directory. Default: runs/benchmark_<timestamp>.

    Returns:
        Aggregate summary dict.
    """
    # Configure concurrency limits
    set_lean_concurrency(lean_concurrency)

    # Output directory
    if out_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(settings.runs_dir) / f"benchmark_{ts}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load records
    records = load_records(jsonl_path, limit=limit)
    total = len(records)
    logger.info(
        "run_benchmark: %d records, workers=%d, lean_concurrency=%d, out=%s",
        total, workers, lean_concurrency, out_dir,
    )

    # Streaming output: write each result as it completes
    summary_jsonl_path = out_dir / "summary.jsonl"
    file_lock = threading.Lock()
    completed_count = 0

    results_list: list[dict] = []

    wall_start = time.time()

    with open(summary_jsonl_path, "w", encoding="utf-8") as summary_file:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(run_one, rec, settings, mode, difficulty_override): rec
                for rec in records
            }

            for future in as_completed(futures):
                result = future.result()
                results_list.append(result)

                # Write to streaming JSONL
                with file_lock:
                    completed_count += 1
                    k = completed_count
                    summary_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                    summary_file.flush()

                status = "success" if result["success"] else "fail"
                elapsed = result.get("elapsed_s", 0.0)
                logger.info(
                    "[%d/%d] %s -> %s (%.1fs)",
                    k, total, result["id"], status, elapsed,
                )
                print(
                    f"[{k}/{total}] {result['id']} -> {status} ({elapsed:.1f}s)",
                    flush=True,
                )

    wall_elapsed = time.time() - wall_start

    # Aggregate stats
    solved_total = sum(1 for r in results_list if r["success"])

    by_difficulty: dict[str, dict] = {}
    for r in results_list:
        diff = r.get("difficulty") or "unknown"
        if diff not in by_difficulty:
            by_difficulty[diff] = {"solved": 0, "total": 0}
        by_difficulty[diff]["total"] += 1
        if r["success"]:
            by_difficulty[diff]["solved"] += 1

    failed_ids = [
        {"id": r["id"], "reason": r.get("reason", "")}
        for r in results_list if not r["success"]
    ]

    aggregate = {
        "total": total,
        "solved": solved_total,
        "wall_clock_s": wall_elapsed,
        "workers": workers,
        "lean_concurrency": lean_concurrency,
        "mode": mode,
        "by_difficulty": by_difficulty,
        "failed": failed_ids,
    }

    # Write aggregate summary.json
    summary_json_path = out_dir / "summary.json"
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(aggregate, f, indent=2, ensure_ascii=False)

    logger.info(
        "Benchmark complete: %d/%d solved in %.1fs. Summary: %s",
        solved_total, total, wall_elapsed, summary_json_path,
    )

    return aggregate
