"""Command-line interface for LocalGoedelArchitect."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from local_goedel.config import load_settings
from local_goedel.orchestrator.pipeline import Pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LocalGoedelArchitect: Lean 4 theorem prover pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  uv run python -m local_goedel.cli problem.lean --difficulty easy --max-iter 4"
        ),
    )
    parser.add_argument(
        "theorem_file",
        type=Path,
        help="Path to the .lean theorem file to prove",
    )
    parser.add_argument(
        "--difficulty",
        choices=["easy", "hard"],
        default="easy",
        help="Difficulty level (affects max iterations)",
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

    args = parser.parse_args()

    theorem_file = args.theorem_file
    if not theorem_file.exists():
        print(f"ERROR: theorem file not found: {theorem_file}", file=sys.stderr)
        return 1

    # Load settings
    env_path = args.env if args.env.exists() else None
    settings = load_settings(env_path=env_path)

    if not settings.api_key:
        print("ERROR: No API key found (set DEEPSEEK_API_KEY in .env or environment)", file=sys.stderr)
        return 1

    # Check Lean server
    from local_goedel.clients.lean_client import LeanClient
    lean_client = LeanClient(settings)
    if not lean_client.health():
        print("WARNING: Lean server not responding at", settings.lean_server_url, file=sys.stderr)

    print(f"Proving: {theorem_file}")
    print(f"Difficulty: {args.difficulty}")
    if args.max_iter:
        print(f"Max iterations: {args.max_iter}")

    # Run pipeline
    pipeline = Pipeline(settings)
    try:
        outcome = pipeline.run(
            theorem_file_path=theorem_file,
            difficulty=args.difficulty,
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
            for run_dir in sorted(runs_dir.iterdir()):
                final = run_dir / "final.lean"
                if final.exists():
                    last_final = final
            try:
                print(f"Final proof: {last_final}")
            except NameError:
                pass
        return 0
    else:
        print(f"FAILED: {outcome.reason}")
        print(f"Iterations used: {outcome.iterations_used}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
