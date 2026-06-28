"""Command-line interface for LocalGoedelArchitect."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from local_goedel.config import load_settings
from local_goedel.orchestrator.pipeline import Pipeline


# ---------------------------------------------------------------------------
# Benchmark subcommand
# ---------------------------------------------------------------------------

def benchmark_main(argv: list[str]) -> int:
    """Entry point for: python -m local_goedel.cli benchmark <jsonl> [opts]"""
    parser = argparse.ArgumentParser(
        prog="local_goedel.cli benchmark",
        description="Run the proving pipeline over a JSONL benchmark file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  uv run python -m local_goedel.cli benchmark bench.jsonl "
            "--mode tool_loop --workers 2"
        ),
    )
    parser.add_argument(
        "jsonl",
        type=Path,
        help="Path to benchmark JSONL file",
    )
    parser.add_argument(
        "--mode",
        choices=["full", "tool_loop", "oneshot", "compile_loop"],
        default="full",
        help="Pipeline mode (default: full)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of parallel worker threads (default: 2)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        dest="out_dir",
        help="Output directory for summary files (default: runs/benchmark_<timestamp>)",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=1,
        help="pass@k: max attempts per problem, early-stop on first success (default: 1)",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--prover-max-tool-calls",
        type=int,
        default=None,
        help="Override prover max tool calls (default: Settings value, 40)",
    )
    parser.add_argument(
        "--prover-max-turns",
        type=int,
        default=None,
        help="Override prover max turns (default: Settings value, 60)",
    )

    args = parser.parse_args(argv)

    if not args.jsonl.exists():
        print(f"ERROR: JSONL file not found: {args.jsonl}", file=sys.stderr)
        return 1

    env_path = args.env if args.env.exists() else None
    settings = load_settings(env_path=env_path)

    if args.prover_max_tool_calls is not None:
        settings.prover_max_tool_calls = args.prover_max_tool_calls
    if args.prover_max_turns is not None:
        settings.prover_max_turns = args.prover_max_turns

    if not settings.api_key:
        print(
            "ERROR: No API key found (set DEEPSEEK_API_KEY in .env or environment)",
            file=sys.stderr,
        )
        return 1

    print(f"Benchmark: {args.jsonl}")
    print(f"Mode: {args.mode}")
    print(f"Workers: {args.workers}")
    print(f"pass@k: {args.k}")
    print(f"Prover budget: max_tool_calls={settings.prover_max_tool_calls} max_turns={settings.prover_max_turns}")
    print()

    from local_goedel.benchmark.runner import run_benchmark

    aggregate = run_benchmark(
        jsonl_path=args.jsonl,
        settings=settings,
        mode=args.mode,
        workers=args.workers,
        out_dir=args.out_dir,
        k=args.k,
    )

    print()
    print("=== Benchmark Summary ===")
    print(json.dumps(aggregate, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Single-problem invocation (original main)
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="LocalGoedelArchitect: Lean 4 theorem prover pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  uv run python -m local_goedel.cli problem.lean --max-iter 4"
        ),
    )
    parser.add_argument(
        "theorem_file",
        type=Path,
        help="Path to the .lean theorem file to prove",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=None,
        help="Override maximum number of iterations",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--prover-max-tool-calls",
        type=int,
        default=None,
        help="Override prover max tool calls (default: Settings value, 40)",
    )
    parser.add_argument(
        "--prover-max-turns",
        type=int,
        default=None,
        help="Override prover max turns (default: Settings value, 60)",
    )

    args = parser.parse_args()

    theorem_file = args.theorem_file
    if not theorem_file.exists():
        print(f"ERROR: theorem file not found: {theorem_file}", file=sys.stderr)
        return 1

    # Load settings
    env_path = args.env if args.env.exists() else None
    settings = load_settings(env_path=env_path)

    if args.prover_max_tool_calls is not None:
        settings.prover_max_tool_calls = args.prover_max_tool_calls
    if args.prover_max_turns is not None:
        settings.prover_max_turns = args.prover_max_turns

    if not settings.api_key:
        print("ERROR: No API key found (set DEEPSEEK_API_KEY in .env or environment)", file=sys.stderr)
        return 1

    # Check Lean server
    from local_goedel.clients.lean_client import LeanClient
    lean_client = LeanClient(settings)
    if not lean_client.health():
        print("WARNING: Lean server not responding at", settings.lean_server_url, file=sys.stderr)

    print(f"Proving: {theorem_file}")
    if args.max_iter:
        print(f"Max iterations: {args.max_iter}")

    # Run pipeline
    pipeline = Pipeline(settings)
    try:
        outcome = pipeline.run(
            theorem_file_path=theorem_file,
            max_iter=args.max_iter,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Print summary
    print()
    if outcome.success:
        print("SUCCESS: Theorem proved!")
        print(f"Iterations used: {outcome.iterations_used}")
        if outcome.final_code:
            # Determine runs dir
            from pathlib import Path as P
            runs_dir = P(settings.runs_dir)
            # Find the final.lean file
            last_final = None
            for run_dir in sorted(runs_dir.iterdir()):
                final = run_dir / "final.lean"
                if final.exists():
                    last_final = final
            if last_final is not None:
                print(f"Final proof: {last_final}")
        return 0
    else:
        print(f"FAILED: {outcome.reason}")
        print(f"Iterations used: {outcome.iterations_used}")
        return 1


# ---------------------------------------------------------------------------
# Entry point — route to benchmark or single-problem
# ---------------------------------------------------------------------------

def entrypoint() -> int:
    """Dispatch to benchmark or single-problem mode based on first argument."""
    if len(sys.argv) >= 2 and sys.argv[1] == "benchmark":
        return benchmark_main(sys.argv[2:])
    return main()


if __name__ == "__main__":
    sys.exit(entrypoint())
