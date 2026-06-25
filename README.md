# LocalGoedelArchitect

A local Python reproduction of the **Goedel-Architect** blueprint-generation + theorem-proving + refinement pipeline for Lean 4 / Mathlib theorems.

## Overview

Goedel-Architect (Liao et al., 2025) is a framework that uses large language models to
decompose hard mathematical theorems into a *blueprint* (a dependency graph of lemmas),
prove each node independently, and refine the blueprint when nodes fail. This
repository implements the same control flow in Python, targeting a
[kimina-lean-server](https://github.com/project-numina/kimina-lean-server) for Lean
verification and a DeepSeek LLM (or any OpenAI-compatible endpoint) for generation.

**Key adaptation from the paper:** The paper relies on a custom Lean extension
(`Architect`, `@[blueprint]` attribute, `sorry_using`) that is not publicly available.
This implementation instead represents the blueprint as a Python graph
(`domain/blueprint.py`) and verifies each node by assembling a standard Lean file
(`import Mathlib` + proved dependencies + the target node) and checking it through the
kimina REPL.

**Anti-cheat acceptance gate (paper §C.2).** A run only "registers a solve" when a
*self-contained proof of the canonical theorem* compiles. The system rebuilds the
submission under the original canonical statement: only the `:= by …` proof **body** is
kept from the prover; the `import`/`set_option`/`open` lines and any pre-declarations come
from the canonical formal statement; all helper lemmas must be folded into the body as
`have` (no extra top-level declarations); and `axiom`/`native_decide`/extra `import`/`open`
are rejected. The proof must additionally be sorry-free and use only whitelisted axioms
(`propext`, `Classical.choice`, `Quot.sound`). This prevents a prover from "cheating" by
altering the theorem statement itself. See `assembly/canonical.py`.

## Architecture

### Module map

```
src/local_goedel/
  config.py               Settings dataclass; PowerShell .env loader; mode + default_max_iter
  cli.py                  Entry point: single-problem `main()` + `benchmark` subcommand
  __main__.py             `python -m local_goedel.cli` dispatcher
  logging_utils.py        Per-run log isolation via a ContextVar routing handler
  telemetry.py            RunTelemetry (tokens + tool counts) bound per run via ContextVar
  domain/
    blueprint.py          Blueprint graph; topo_sort, ready_nodes, is_solved
    node.py               BlueprintNode (status, proof, diagnosis, attempts, ...)
    lean_check.py         CheckResult / LeanMessage parsed from kimina (HTTP + kimina-client)
    results.py            ProofResult / Diagnosis / DiagnosisKind
  clients/
    lean_client.py        kimina-lean-server client (kimina-client preferred, HTTP fallback)
    llm_client.py         OpenAI-compatible LLM client (thinking + tools; tenacity retries)
    mathlib_client.py     Mathlib search REST (rate-limited, retried)
  assembly/
    lean_assembler.py     build_node_file / parse_theorem_file (def + theorem rendering)
    axiom_check.py        uses_sorry: inspects #print axioms output for sorryAx
    canonical.py          split_canonical / build_canonical_submission / scan_proof_body /
                          axiom_whitelist_ok  (the anti-cheat finalization gate)
  tools/
    base.py               ToolRegistry / ToolContext / ToolResult (schemas(allowed) filter)
    lean_compile.py       lean_compile tool (proof_body / full_file / canonical modes)
    mathlib_search.py     mathlib_search tool (wraps the search client)
  agents/
    agent.py              Generic tool-calling agent loop (AgentConfig.allowed_tools)
    prompts.py            System prompts: generator, prover, refiner, diagnosis, synthesizer, oneshot
    blueprint_generator.py  LLM-based blueprint generation (with skeleton typecheck)
    blueprint_refiner.py    LLM-based blueprint refinement (applies patch ops)
    parsers.py            JSON blueprint parser / patch parser
    prover.py             Prover: drives tool-use loop per node, runs diagnosis
    synthesizer.py        Synthesizer: produces ONE canonical proof body, gated by canonical.py
  orchestrator/
    pipeline.py           Top-level run(); dispatches full / tool_loop / compile_loop / oneshot
    scheduler.py          prove_wave: serial wave dispatch
    state.py              IterationRecord / RunOutcome
    artifacts.py          ArtifactWriter: saves blueprints, node results, final.lean, outcome
  benchmark/
    runner.py             JSONL benchmark: load_records / run_one / run_problem_passk / run_benchmark
```

### LLM roles

| Role | Agent | What it does |
|---|---|---|
| Blueprint Generator | `BlueprintGenerator` | Decomposes a theorem signature into a JSON dependency graph; type-checks the skeleton via kimina |
| Prover | `Prover` | Tool-calling loop to find a proof body for one node; runs `lean_compile` + `mathlib_search`; produces a Diagnosis on failure |
| Refiner | `BlueprintRefiner` | Given the blueprint + all diagnoses, emits JSON patch ops (add_node, replace_statement, rewire, drop_node, decompose) to restructure the graph |
| Synthesizer | `Synthesizer` | Produces ONE self-contained `:= by …` body for the canonical theorem (helpers inlined as `have`), gated by the anti-cheat canonical check |

All roles use a single DeepSeek model (`deepseek-v4-flash` by default) with **thinking
enabled** (`enable_thinking=True`): the API call sends `tools`, `reasoning_effort="high"`,
and `extra_body={"thinking":{"type":"enabled"}}` together, and the response carries
`message.reasoning_content`.

### Tools

| Tool | Description |
|---|---|
| `lean_compile` | Submit a proof body / full file / canonical submission to kimina; returns errors/warnings/status |
| `mathlib_search` | Search for relevant Mathlib lemmas by keyword |

### Ablation modes

The pipeline supports four modes, all ending at the **same canonical acceptance gate**:

| Mode | Blueprint? | Tools exposed | Notes |
|---|---|---|---|
| `full` | yes | `lean_compile` + `mathlib_search` | Blueprint → prove waves → refine → canonical synthesis (default) |
| `tool_loop` | no | `lean_compile` + `mathlib_search` | Single agent loop directly on the canonical target |
| `compile_loop` | no | `lean_compile` **only** | Like `tool_loop` but `mathlib_search` is filtered out (ablation) |
| `oneshot` | no | none | One LLM completion (no tools); thinking does the work |

For the single-problem CLI the mode is taken from `Settings.mode` (env `MODE`, default
`full`); for the benchmark it is `--mode`.

### Pipeline loop (`full` mode)

```
  theorem (file or canonical statement)
        |
        v
  [BlueprintGenerator] --- skeleton typecheck ---> blueprint (iter 0)
        |
        v
  for iteration in 1..max_iter:
    +---------------------------------------------+
    |  Wave loop (inner):                         |
    |    ready = nodes whose all parents PROVED   |
    |    for node in ready (serial):              |
    |      [Prover] --tools--> lean_compile,      |
    |                          mathlib_search     |
    |      if success: node.status = PROVED       |
    |      else:       run Diagnosis              |
    |    repeat until no progress or solved       |
    +---------------------------------------------+
        |
     solved? --YES--> finalize
        |
       NO --> [BlueprintRefiner] --> updated blueprint
        |
     stuck (no progress AND refiner unchanged)? --YES--> FAILED
        |
       NO --> next iteration

  finalize (anti-cheat canonical gate, shared by all modes):
    [Synthesizer] --> ONE canonical proof body (helpers as `have`)
    build_canonical_submission(canonical, body) + #print axioms
    lean_client.check  -->  status == valid?
    scan_proof_body (no top-level decls / axiom / native_decide / extra imports)?
    axiom_whitelist_ok (axioms subset of {propext, Classical.choice, Quot.sound})?
    all pass --> SUCCESS   else --> FAILED (SAFEGUARD / AXIOM / lean_error)
```

**Ready-node gating:** A node is dispatched only once all its declared parents have
status PROVED (bottom-up). This differs from the paper's `sorry_using` approach, which can
assume unproved parent results and prove nodes in parallel.

## Prerequisites

### kimina-lean-server

The pipeline talks to a kimina-lean-server. Locally you can run the container (Docker):

```powershell
docker run -d --name kimina -p 8000:8000 `
  -e LEAN_SERVER_MAX_REPL_MEM=12G `
  -e LEAN_SERVER_INIT_REPLS='{"import Mathlib":1}' `
  kimina-lean-server:v4.26.0-offline
```

Wait ~60 seconds for the Mathlib environment to initialise (check `docker logs kimina`).
Communication uses the `kimina-client` pip package (a fresh REPL per check, `reuse=False`),
with a raw HTTP fallback. To target a **remote / cluster** kimina server, set
`LEAN_SERVER_URL` in `.env` (otherwise it defaults to `http://localhost:8000`).

### .env file (PowerShell format)

Create `.env` in the repo root:

```powershell
$env:MODEL_NAME       = "deepseek-v4-flash"
$env:BASE_URL         = "https://api.deepseek.com"
$env:DEEPSEEK_API_KEY = "sk-..."
# Optional — point at cluster / self-built services (fall back to localhost / public):
# $env:LEAN_SERVER_URL = "http://<cluster-host>:<port>"
# $env:LEANDEX_URL     = "http://<your-mathlib-search-host>/..."
# Optional — single-problem mode override (default "full"):
# $env:MODE            = "compile_loop"
```

> Never commit `.env` or real API keys — `.env` is gitignored.

### Python dependencies

Requires Python >= 3.11 and [uv](https://github.com/astral-sh/uv):

```powershell
uv sync
```

On Windows, set `$env:PYTHONUTF8=1` before running so Unicode Lean output (ℝ, ⊢, ≥) does
not crash the GBK console.

## Usage — single problem

Prove one `.lean` file (one theorem per file):

```powershell
uv run python -m local_goedel.cli <theorem_file.lean> [--max-iter N] [--env .env]
```

- `--max-iter N` overrides the iteration budget (defaults to `Settings.default_max_iter`).
- The mode is taken from `Settings.mode` (env `MODE`, default `full`) — the single-problem
  CLI has no `--mode` flag.

```powershell
uv run python -m local_goedel.cli lean4_problems/Minif2f/algebra_sqineq_2atp2bpge2ab.lean --max-iter 4
```

Single-problem runs write artifacts to `runs/<problem>_<NNN>/`:

| File | Description |
|---|---|
| `blueprint.iter00.json` | Initial blueprint |
| `blueprint.iterNN.refined.json` | Blueprint after each refinement |
| `iterNN/node_<id>.result.json` | Per-node proof result + diagnosis |
| `final.lean` | Accepted canonical submission (proof body under the canonical statement) |
| `final.check.json` | kimina check result for the final file |
| `outcome.json` | `{success, reason, iterations_used, mode}` |
| `run.log` | Full DEBUG-level pipeline log (isolated per run) |

## Benchmark

Run the pipeline over many problems from a structured JSONL file, in parallel, with
**pass@k** and full **telemetry**.

```powershell
uv run python -m local_goedel.cli benchmark <benchmark.jsonl> `
  --mode {full|tool_loop|compile_loop|oneshot} `
  --workers N `
  --k K `
  [--out runs/my_bench] [--env .env]
```

| Flag | Meaning |
|---|---|
| `--mode` | Ablation mode for every problem (`full` / `tool_loop` / `compile_loop` / `oneshot`). Default `full`. |
| `--workers N` | Problem-level parallelism (a `ThreadPoolExecutor`). Different problems run concurrently. This is the only concurrency knob. Default `2`. |
| `--k K` | pass@k: attempt each problem up to `K` times, **stopping at the first success**. Default `1`. |
| `--out DIR` | Output directory. Default `runs/benchmark_<timestamp>/`. |
| `--env PATH` | Path to the `.env` file. Default `.env`. |

### Input format (JSONL)

One JSON object per line. The runner consumes only **four** fields; any other fields
(`source`, `nl_statement`, `difficulty`, judge flags, …) are carried as metadata and
**ignored**:

| Field | Used for |
|---|---|
| `id` | Problem identifier (becomes the run-dir base name and the `id` in the summary) |
| `header` | The canonical `import` / `set_option` / `open` lines |
| `formal_statement` | The canonical statement: optional pre-declarations (`def …`) + the target `theorem … := by sorry` |
| `lean4_code` | The full input file (header + statement). Used as the fallback for splitting if `formal_statement`/`header` are absent. |

A real record from `lean4_problems/benchmark/benchmark_examples.jsonl` (metadata fields
elided), which also exercises the pre-declaration path:

```json
{
  "id": "0f311a87-4306-5b7e-abb2-b03330456256",
  "header": "import Mathlib",
  "formal_statement": "def Heidi : ℝ := 2.1\ndef Lola : ℝ := 1.4\ntheorem arithmetic_4185 : (Heidi + Lola) / 2 = 1.75 := by sorry",
  "lean4_code": "import Mathlib\ndef Heidi : ℝ := 2.1\ndef Lola : ℝ := 1.4\ntheorem arithmetic_4185 : (Heidi + Lola) / 2 = 1.75 := by sorry"
}
```

Here `split_canonical` yields header `import Mathlib`, pre-decls `def Heidi` / `def Lola`,
and the target theorem `arithmetic_4185` — and the accepted proof is rebuilt as
`header + pre_decls + theorem arithmetic_4185 … := <body>` (see the anti-cheat gate above).

### How it works (method)

```
load_records(jsonl)                         # read every line; skip blanks/malformed
   |
   v
ThreadPoolExecutor(max_workers=N)           # one future per PROBLEM (parallel)
   |
   +-- run_problem_passk(record, mode, k):  # pass@k, SERIAL within a problem
   |       for attempt in 1..k:
   |           run_one(record, mode):
   |               split_canonical(lean4_code, formal_statement, header)
   |               bind a fresh RunTelemetry (ContextVar, per attempt)
   |               Pipeline(settings).run(canonical=…, mode=…)   --> RunOutcome
   |           if attempt succeeded: BREAK   # early-stop saves work
   |       sum telemetry + elapsed over the attempts run; record attempts_used
   |
   v
stream each problem result -> summary.jsonl (as it completes)
aggregate -> summary.json   (pass@k solved/total, attempts distribution, token+tool totals)
```

- **pass@k is serial per problem with early-stop**: as soon as one attempt of a problem
  passes the canonical gate, the remaining attempts are skipped. `attempts_used` records
  how many actually ran (≤ k). Parallelism happens across *different* problems via
  `--workers`.
- **All four modes share the same canonical acceptance gate**, so pass@k and telemetry
  work identically for every mode.

### Running it

```powershell
$env:PYTHONUTF8 = "1"
uv run python -m local_goedel.cli benchmark lean4_problems/benchmark/benchmark_examples.jsonl `
  --mode compile_loop --workers 2 --k 4
```

Console output streams one line per problem as it completes, then prints the aggregate:

```
Benchmark: lean4_problems/benchmark/benchmark_examples.jsonl
Mode: compile_loop
Workers: 2
pass@k: 4

[1/5] 0f311a87-4306-5b7e-abb2-b03330456256 -> success (4.2s, attempts=1/4)
[2/5] Imo1982P3a -> fail (61.0s, attempts=4/4)
...
=== Benchmark Summary ===
{ ...aggregate... }
```

> **Local smoke test (limited resources):** point at a small JSONL with one or two problems
> and use `--workers 1 --k 2` to confirm functionality without heavy load.

### Outputs

Benchmark runs write to `runs/benchmark_<timestamp>/` (or `--out`):

- **`summary.jsonl`** — one line per problem, streamed as it completes:

  ```json
  {"id": "0f311a87-...", "mode": "compile_loop", "k": 4, "passed": true,
   "success": true, "attempts_used": 1, "reason": "...", "elapsed_s": 4.2,
   "telemetry": {"prompt_tokens": 2013, "completion_tokens": 148,
                 "reasoning_tokens": 79, "total_tokens": 2161,
                 "tool_calls": {"lean_compile": 1}},
   "attempts": [ { "attempt": 1, "success": true, "reason": "...",
                   "elapsed_s": 4.2, "run_dir": "runs/0f311a87-..._001",
                   "telemetry": { ... } } ]}
  ```
  (`success` is a backward-compat alias of `passed`. Per-attempt artifacts live under each
  attempt's own `run_dir`, exactly like a single-problem run.)

- **`summary.json`** — the aggregate:

  ```json
  {
    "total": 5,
    "solved": 1,
    "k": 4,
    "wall_clock_s": 132.4,
    "workers": 2,
    "mode": "compile_loop",
    "total_attempts": 13,
    "attempts_distribution": {"1": 1, "4": 4},
    "failed": [{"id": "Imo1982P3a", "reason": "..."}],
    "telemetry_total": {
      "prompt_tokens": 40231,
      "completion_tokens": 5120,
      "reasoning_tokens": 2890,
      "total_tokens": 45351,
      "tool_calls": {"lean_compile": 22, "mathlib_search": 0}
    }
  }
  ```

### Telemetry

Every benchmark run records, per problem and in aggregate:

- **Token budget** — `prompt_tokens`, `completion_tokens`, and `reasoning_tokens` (read
  from `usage.completion_tokens_details.reasoning_tokens`; reasoning is a *subset* of
  completion, so `total_tokens = prompt + completion`).
- **pass@k attempts actually run** — `attempts_used` per problem, plus
  `attempts_distribution` and `total_attempts` in the aggregate.
- **Per-tool call counts** — `tool_calls` (e.g. `lean_compile`, `mathlib_search`),
  collected at the single `ToolRegistry.dispatch` choke point so every mode is covered.

Telemetry is accumulated through a `ContextVar` (`telemetry.py`), bound per attempt inside
the worker thread, so counts stay correct under parallel `--workers`.

## Testing

```powershell
# Unit tests only (fast, no services required) — 254 tests
uv run pytest tests/unit -q

# Integration tests (require kimina + Mathlib search connectivity)
uv run pytest tests/integration -q

# End-to-end tests (require kimina + API key; marked @pytest.mark.slow)
uv run pytest tests/e2e -q
```

Integration tests auto-skip when services are unreachable (via `pytest.skip` in
`autouse=True` fixtures).

## Known limitations

1. **Multi-theorem files not supported (single-problem CLI).** `parse_theorem_file` raises
   `ValueError` if a `.lean` file contains more than one `theorem`/`lemma` declaration. Run
   each theorem in a separate file. (The JSONL benchmark format sidesteps this by carrying
   one canonical statement per record.)

2. **Ready-node gating differs from the paper.** Nodes are dispatched only once all parent
   nodes are PROVED (bottom-up). The paper's `sorry_using` allows proving a node while
   assuming unproved parents, enabling broader parallelism and earlier feedback. Extending
   to the paper's parallelism is future work.

3. **`full`-mode synthesis on hard problems.** Strict single-theorem finalization requires
   the Synthesizer to fold all proved helper lemmas into one canonical body as `have`. On
   hard problems this final step can fail even when every helper lemma was proved
   individually — the accepted, faithful-to-paper tradeoff. The ablation modes
   (`tool_loop`, `compile_loop`, `oneshot`) surface this by avoiding the blueprint.

4. **Mathlib search capacity.** The `mathlib_search` tool is rate-limited (a min
   inter-request interval) and treated as best-effort (returns `[]` on failure, never
   fatal). Heavy parallel use or a slow service may throttle.

5. **Putnam difficulty.** Putnam-level problems require deep mathematical insight that
   current models cannot reliably express as Lean 4 tactic proofs within a few iterations;
   they remain stretch goals.
```
