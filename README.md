# LocalGoedelArchitect

A local Python reproduction of the **Goedel-Architect** blueprint-generation + theorem-proving + refinement pipeline for Lean 4 / Mathlib theorems.

## Overview

Goedel-Architect (Liao et al., 2025) is a framework that uses large language models to
decompose hard mathematical theorems into a *blueprint* (a dependency graph of lemmas),
prove each node independently, and refine the blueprint when nodes fail. This
repository implements the same control flow in Python, targeting a locally-run
[kimina-lean-server](https://github.com/project-numina/kimina-lean-server) for Lean
verification and a DeepSeek LLM (or any OpenAI-compatible endpoint) for generation.

**Key adaptation from the paper:** The paper relies on a custom Lean extension
(`Architect`, `@[blueprint]` attribute, `sorry_using`) that is not publicly available.
This implementation instead represents the blueprint as a Python graph
(`domain/blueprint.py`) and verifies each node by assembling a standard Lean file
(`import Mathlib` + sorry-stubs for dependencies + the target node) and checking it
through the kimina REPL. The final proof is verified sorry-free via `#print axioms`.

## Architecture

### Module map

```
src/local_goedel/
  config.py               Settings dataclass; PowerShell .env loader
  cli.py                  Entry point: `python -m local_goedel.cli`
  domain/
    blueprint.py          Blueprint graph; topo_sort, ready_nodes, is_solved
    node.py               BlueprintNode dataclass (status, proof, diagnosis, ...)
    lean_check.py         CheckResult / LeanMessage parsed from kimina JSON
    results.py            ProofResult / Diagnosis / DiagnosisKind
  clients/
    lean_client.py        HTTP client for kimina-lean-server /check endpoint
    llm_client.py         OpenAI-compatible LLM client (tenacity retries)
    mathlib_client.py     Leandex REST search (rate-limited, retried)
  assembly/
    lean_assembler.py     build_node_file / build_final_file / parse_theorem_file
    axiom_check.py        uses_sorry: inspects #print axioms output for sorryAx
  tools/
    base.py               ToolRegistry / ToolContext / ToolResult
    lean_compile.py       lean_compile tool (proof_body or full_file mode)
    mathlib_search.py     mathlib_search tool (wraps MathlibSearchClient)
  agents/
    agent.py              Generic tool-calling agent loop
    prompts.py            System prompts for generator, prover, refiner, diagnosis
    blueprint_generator.py  LLM-based blueprint generation (with skeleton typecheck)
    blueprint_refiner.py    LLM-based blueprint refinement (applies patch ops)
    parsers.py            JSON blueprint parser / patch parser
    prover.py             Prover: drives tool-use loop per node, runs diagnosis
  orchestrator/
    pipeline.py           Top-level generate -> prove-waves -> refine -> finalize
    scheduler.py          prove_wave: serial wave dispatch
    state.py              IterationRecord / RunOutcome
    artifacts.py          ArtifactWriter: saves blueprints, node results, final.lean
```

### Three LLM roles

| Role | Agent | What it does |
|---|---|---|
| Blueprint Generator | `BlueprintGenerator` | Decomposes a theorem signature into a JSON dependency graph; type-checks skeleton via kimina |
| Prover | `Prover` | Tool-calling loop to find a proof body for one node; runs lean_compile and mathlib_search; produces a Diagnosis on failure |
| Refiner | `BlueprintRefiner` | Given the current blueprint + all diagnoses, emits JSON patch operations (add_node, replace_statement, rewire, drop_node, decompose) to restructure the graph |

### Two tools (available to the Prover)

| Tool | Description |
|---|---|
| `lean_compile` | Submit a proof body or full Lean file to kimina-lean-server; returns errors/warnings |
| `mathlib_search` | Search Leandex for relevant Mathlib lemmas by keyword |

### Pipeline loop (ASCII diagram)

```
  theorem_file.lean
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
    |      else:       node.status = UNPROVED     |
    |                  run Diagnosis              |
    |    repeat until no progress or solved       |
    +---------------------------------------------+
        |
     solved? --YES--> finalize
        |
       NO
        |
        v
  [BlueprintRefiner] --> updated blueprint (refined)
        |
     stuck (no progress 2+ iters AND refiner unchanged)? --YES--> FAILED
        |
       NO --> next iteration
        |
  finalize:
    build_final_file (topo order, real proofs)
    lean_client.check(final_code)
    uses_sorry? --NO--> SUCCESS
                 YES--> FAILED
```

**Ready-node gating:** A node is dispatched only once all its declared parents have
status PROVED. This bottom-up strategy differs from the paper's `sorry_using` approach,
which can assume unproved parent results and prove nodes in parallel.

## Prerequisites

### kimina-lean-server

Pull and run the container (requires Docker):

```powershell
docker run -d --name kimina -p 8000:8000 `
  -e LEAN_SERVER_MAX_REPL_MEM=12G `
  -e LEAN_SERVER_INIT_REPLS='{"import Mathlib":1}' `
  kimina-lean-server:v4.26.0-offline
```

Wait ~60 seconds for the Mathlib environment to initialise (check `docker logs kimina`).

### .env file (PowerShell format)

Create `.env` in the repo root:

```powershell
$env:MODEL_NAME   = "deepseek-chat"
$env:BASE_URL     = "https://api.deepseek.com"
$env:DEEPSEEK_API_KEY = "sk-..."
```

### Python dependencies

Requires Python >= 3.11 and [uv](https://github.com/astral-sh/uv):

```powershell
uv sync
```

## Usage

```powershell
uv run python -m local_goedel.cli <theorem_file.lean> [--difficulty easy|hard] [--max-iter N]
```

**Examples:**

```powershell
# Easy problem, up to 4 iterations
uv run python -m local_goedel.cli lean4_problems/Minif2f/algebra_sqineq_2atp2bpge2ab.lean --difficulty easy --max-iter 4

# Harder problem (Putnam), 3-iteration smoke test
uv run python -m local_goedel.cli lean4_problems/Putnam2025/putnam_2025_a1.lean --difficulty hard --max-iter 3
```

**Artifacts** are written to `runs/<problem>_<NNN>/`:

| File | Description |
|---|---|
| `blueprint.iter00.json` | Initial blueprint |
| `blueprint.iterNN.refined.json` | Blueprint after each refinement |
| `iterNN/node_<id>.result.json` | Per-node proof result + diagnosis |
| `final.lean` | Assembled final Lean file |
| `final.check.json` | kimina check result for final file |
| `outcome.json` | `{success, reason, iterations_used}` |
| `run.log` | Full DEBUG-level pipeline log |

## Testing

```powershell
# Unit tests only (fast, no services required)
uv run pytest tests/unit -q

# Integration tests (require kimina at localhost:8000 and Leandex connectivity)
uv run pytest tests/integration -q

# End-to-end tests (require kimina + API key; marked @pytest.mark.slow)
uv run pytest tests/e2e -q
```

Integration tests auto-skip when services are unreachable (using `pytest.skip` in
`autouse=True` fixtures). The e2e test `test_full_pipeline_algebra_sqineq` uses
`max_iter=2`, which can be insufficient on some LLM runs (nondeterministic); this is a
known flakiness issue rather than a service-gating bug.

## Results

### MiniF2F benchmark (`--difficulty easy --max-iter 4`)

| Problem | Solved? | Iters | Nodes (init) | Refinement summary | Wall time |
|---|---|---|---|---|---|
| `algebra_sqineq_2atp2bpge2ab` | YES | 3 | 2 | Iter 1: sub_sq_nonneg failed (linarith insufficient); refiner added square_expand helper. Iter 2: sub_sq_nonneg proved; target failed; refiner updated proof_sketch. Iter 3: both proved. | ~4:36 |
| `mathd_algebra_478` | YES | 2 | 5 | Iter 1: factor_2009/h2009_ne_zero/sub_one_eq_2008 proved; cancel lemma failed; refiner updated proof_sketch. Iter 2: cancel proved, target proved. | ~1:27 |
| `mathd_numbertheory_284` | YES | 1 | 1 | No refinement needed. Single node proved by native_decide. | ~1:26 |
| `amc12a_2021_p7` | YES | 1 | 1 | No refinement needed. Single node proved by native_decide. | ~0:35 |
| `imo_1964_p1` | SKIPPED | - | - | File contains 2 theorem declarations (imo_1964_p1_a, imo_1964_p1_b). parse_theorem_file raises ValueError; multi-theorem files are not supported. | - |

**Final proofs (key excerpts):**

`algebra_sqineq_2atp2bpge2ab` (3 nodes, 12 lines):
```lean
theorem square_expand (a b : Real) : (a - b)^2 = a ^ 2 + b ^ 2 - 2 * a * b := by ring
theorem sub_sq_nonneg (a b : Real) : (a - b)^2 >= 0 := sq_nonneg (a - b)
theorem algebra_sqineq_2atp2bpge2ab (a b : Real) : a ^ 2 + b ^ 2 >= 2 * a * b := by
  have h := sub_sq_nonneg a b
  have h_expand : (a - b)^2 = a ^ 2 + b ^ 2 - 2 * a * b := square_expand a b
  linarith
```

`mathd_numbertheory_284` (1 node):
```lean
theorem mathd_numbertheory_284 :
    Nat.gcd (2 ^ 1001 - 1) (2 ^ 1012 - 1) = 2 ^ 11 - 1 := by native_decide
```

### Putnam 2025 stretch attempt (`putnam_2025_a1`, `--difficulty hard --max-iter 3`)

| Field | Value |
|---|---|
| Outcome | FAILED (STUCK after 2 iterations) |
| Wall time | ~14:40 |
| Blueprint | 9 nodes: delta_def (def), oddPart_def (def), oddPart_mul_two, oddPart_div_odd, gcd_divides_delta, delta_recurrence, oddPart_nonincrease, oddPart_strict_decrease, putnam_2025_a1 (target) |
| Strategy | Show odd part of abs(2m_k+1 - 2n_k+1) is non-increasing and strictly decreases when gcd != 1; finiteness follows since N has no infinite strictly decreasing chains |
| Nodes that failed | delta_def and oddPart_def in both iterations |
| Failure mode | Both are kind:definition nodes (return a value, not a Prop). build_node_file assembles them as `theorem`; Lean rejects with "type of theorem X is not a proposition". Diagnosis: statement_wrong |
| Refiner behaviour | Iter 1: refiner stripped nl/sketch fields, kept 9-node structure. Iter 2: another 9-node unchanged graph. STUCK detection fired. |

The core blocker is that `build_node_file` always renders definitions as `theorem`
syntax. A `def`-rendering path in `lean_assembler.py` would fix this.

## Known limitations

1. **Multi-theorem files not supported.** `parse_theorem_file` raises `ValueError` if
   the file contains more than one `theorem`/`lemma` declaration. Run each theorem
   in a separate file (e.g., split `imo_1964_p1.lean` into `_a.lean` and `_b.lean`).

2. **Serial proving (single REPL).** The wave scheduler dispatches nodes one at a time
   in topological order. The kimina REPL is stateful and shares a single Lean
   environment, so parallelism is not safe without separate REPL instances.

3. **Leandex rate limits.** The `mathlib_search` tool calls Leandex
   (`leandex.projectnumina.ai`) with a default 2-second inter-request interval and 4
   retries. Heavy parallel use or a slow network may cause throttling.

4. **Thinking mode vs. tool use.** DeepSeek thinking mode (`enable_thinking=True`) is
   disabled by default because the DeepSeek API does not support streaming tool calls
   in thinking mode (the tool schema is dropped). Use `reasoning_effort="high"` instead.

5. **Definition nodes rendered as theorems.** Blueprint nodes with `kind: definition`
   are currently assembled as `theorem` declarations in `build_node_file`. Lean rejects
   function-valued signatures with "type of theorem X is not a proposition". This
   blocks Putnam problems that require helper function definitions.

6. **Ready-node gating differs from the paper.** Nodes are dispatched only once all
   parent nodes are PROVED (bottom-up). The paper's `sorry_using` allows proving a node
   while assuming unproved parents, enabling broader parallelism and earlier feedback.

7. **Putnam difficulty.** Putnam 2025 problems require deep mathematical insight
   that current DeepSeek models cannot reliably express as Lean 4 tactic proofs within
   a few iterations.

8. **E2E test flakiness.** `tests/e2e/test_pipeline_trivial.py` runs with `max_iter=2`,
   which is occasionally insufficient due to LLM nondeterminism. The test is marked
   `@pytest.mark.slow` but is not skipped by default when services are live.
