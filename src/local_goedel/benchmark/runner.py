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
from local_goedel.logging_utils import get_logger
from local_goedel.orchestrator.pipeline import Pipeline
from local_goedel.telemetry import RunTelemetry, telemetry_scope

logger = get_logger(__name__)


def _sum_telemetry(tel_dicts: list[dict]) -> dict:
    """Sum a list of telemetry dicts (from RunTelemetry.as_dict()) into one."""
    acc = RunTelemetry()
    for td in tel_dicts:
        if td is None:
            continue
        partial = RunTelemetry(
            prompt_tokens=td.get("prompt_tokens", 0),
            completion_tokens=td.get("completion_tokens", 0),
            reasoning_tokens=td.get("reasoning_tokens", 0),
            tool_calls=dict(td.get("tool_calls", {})),
        )
        acc.merge(partial)
    return acc.as_dict()


# ---------------------------------------------------------------------------
# Record loading
# ---------------------------------------------------------------------------

def load_records(
    jsonl_path: str | Path,
) -> list[dict]:
    """Load benchmark records from a JSONL file.

    Args:
        jsonl_path: Path to the JSONL file.

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
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Run a single record
# ---------------------------------------------------------------------------

def run_one(
    record: dict,
    settings: object,
    mode: Optional[str],
) -> dict:
    """Run the pipeline on a single benchmark record.

    Args:
        record:   Benchmark record dict (must have "lean4_code").
        settings: Settings object.
        mode:     Pipeline mode override (or None to use settings).

    Returns:
        Result dict with keys:
            id, mode, success, reason, elapsed_s, run_dir
    """
    rec_id = record.get("id") or "unknown"
    start = time.time()

    result: dict = {
        "id": rec_id,
        "mode": mode,
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
            result["telemetry"] = RunTelemetry().as_dict()
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
        tel = RunTelemetry()
        with telemetry_scope(tel):
            outcome = pipeline.run(
                theorem_file_path=dummy_path,
                theorem_src=lean4_code,
                canonical=canon,
                mode=mode,
            )

        elapsed = time.time() - start
        result["success"] = outcome.success
        result["reason"] = outcome.reason
        result["elapsed_s"] = elapsed
        result["telemetry"] = tel.as_dict()

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
        result["telemetry"] = RunTelemetry().as_dict()
        logger.error("run_one(%s) raised: %s", rec_id, exc, exc_info=True)

    return result


# ---------------------------------------------------------------------------
# pass@k: run one problem up to k times, early-stop on first success
# ---------------------------------------------------------------------------
#
# NOTE: `oneshot_retries` (in Settings) is an *internal* retry within a
# single oneshot attempt and is orthogonal to pass@k.  For clean pass@k
# accounting it should normally be 1.  Do NOT change its default here.

def run_problem_passk(
    record: dict,
    settings: object,
    mode: Optional[str],
    k: int,
) -> dict:
    """Attempt a single benchmark problem up to k times (pass@k, early-stop).

    Problem-level parallelism is handled by run_benchmark's ThreadPool; the
    k attempts inside a problem are executed SERIALLY so that early-stop is
    reliable and resource consumption is minimised.

    Args:
        record:   Benchmark record dict.
        settings: Settings object.
        mode:     Pipeline mode override (or None).
        k:        Maximum attempts (pass@k).

    Returns:
        Per-problem result dict with keys:
            id, mode, k, passed, attempts_used, success (alias of passed),
            reason, elapsed_s, telemetry, attempts.
    """
    rec_id = record.get("id") or "unknown"
    attempts: list[dict] = []

    for attempt_idx in range(1, k + 1):
        attempt_result = run_one(record, settings, mode)
        attempts.append(attempt_result)

        if attempt_result["success"]:
            # Early-stop: first success wins
            break

    attempts_used = len(attempts)
    passed = attempts[-1]["success"]
    reason = attempts[-1]["reason"]
    elapsed_s = sum(a.get("elapsed_s", 0.0) for a in attempts)
    telemetry = _sum_telemetry([a.get("telemetry") for a in attempts])

    # Build a compact per-attempt summary for the `attempts` list
    attempts_summary = [
        {
            "attempt": i + 1,
            "success": a["success"],
            "reason": a.get("reason", ""),
            "elapsed_s": a.get("elapsed_s", 0.0),
            "run_dir": a.get("run_dir"),
            "telemetry": a.get("telemetry"),
        }
        for i, a in enumerate(attempts)
    ]

    return {
        "id": rec_id,
        "mode": mode,
        "k": k,
        "passed": passed,
        "success": passed,          # backward-compat alias
        "attempts_used": attempts_used,
        "reason": reason,
        "elapsed_s": elapsed_s,
        "telemetry": telemetry,
        "attempts": attempts_summary,
    }


# ---------------------------------------------------------------------------
# Run the full benchmark
# ---------------------------------------------------------------------------

def run_benchmark(
    jsonl_path: str | Path,
    settings: object,
    mode: Optional[str],
    workers: int = 2,
    out_dir: Optional[str | Path] = None,
    k: int = 1,
) -> dict:
    """Run the benchmark over a JSONL file in parallel.

    Each problem is attempted up to k times (pass@k, early-stop on first
    success).  Problem-level parallelism uses the ThreadPoolExecutor; the k
    serial attempts per problem are handled by run_problem_passk.

    Args:
        jsonl_path: Path to the JSONL file.
        settings:   Settings object.
        mode:       Pipeline mode override.
        workers:    ThreadPoolExecutor worker count.
        out_dir:    Output directory. Default: runs/benchmark_<timestamp>.
        k:          pass@k — max attempts per problem (early-stop on success).

    Returns:
        Aggregate summary dict.
    """
    # Output directory
    if out_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(settings.runs_dir) / f"benchmark_{ts}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load records
    records = load_records(jsonl_path)
    total = len(records)
    logger.info(
        "run_benchmark: %d records, workers=%d, k=%d, out=%s",
        total, workers, k, out_dir,
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
                executor.submit(run_problem_passk, rec, settings, mode, k): rec
                for rec in records
            }

            for future in as_completed(futures):
                result = future.result()
                results_list.append(result)

                # Write to streaming JSONL
                with file_lock:
                    completed_count += 1
                    done_idx = completed_count
                    summary_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                    summary_file.flush()

                # `success` is the backward-compat alias of `passed`
                status = "success" if result["success"] else "fail"
                elapsed = result.get("elapsed_s", 0.0)
                attempts_used = result.get("attempts_used", 1)
                logger.info(
                    "[%d/%d] %s -> %s (%.1fs, attempts=%d/%d)",
                    done_idx, total, result["id"], status, elapsed,
                    attempts_used, k,
                )
                print(
                    f"[{done_idx}/{total}] {result['id']} -> {status}"
                    f" ({elapsed:.1f}s, attempts={attempts_used}/{k})",
                    flush=True,
                )

    wall_elapsed = time.time() - wall_start

    # Aggregate stats — count PROBLEMS (pass@k solved)
    solved_total = sum(1 for r in results_list if r["success"])

    failed_ids = [
        {"id": r["id"], "reason": r.get("reason", "")}
        for r in results_list if not r["success"]
    ]

    # Sum telemetry across all problems (each problem's telemetry is already
    # summed across its attempts by run_problem_passk).
    telemetry_total = _sum_telemetry([r.get("telemetry") for r in results_list])

    # attempts_distribution: how many problems used exactly N attempts
    attempts_dist: dict[str, int] = {}
    for r in results_list:
        key = str(r.get("attempts_used", 1))
        attempts_dist[key] = attempts_dist.get(key, 0) + 1
    total_attempts = sum(r.get("attempts_used", 1) for r in results_list)

    aggregate = {
        "total": total,
        "solved": solved_total,
        "k": k,
        "wall_clock_s": wall_elapsed,
        "workers": workers,
        "mode": mode,
        "total_attempts": total_attempts,
        "attempts_distribution": attempts_dist,
        "failed": failed_ids,
        "telemetry_total": telemetry_total,
    }

    # Write aggregate summary.json
    summary_json_path = out_dir / "summary.json"
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(aggregate, f, indent=2, ensure_ascii=False)

    logger.info(
        "Benchmark complete: %d/%d solved in %.1fs (pass@%d). Summary: %s",
        solved_total, total, wall_elapsed, k, summary_json_path,
    )

    return aggregate
