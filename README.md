# testteeth

**Grade AI-written tests by mutation testing, then hand your coding agent precise briefs for the tests that are missing.**

[![CI](https://github.com/fernandogarzaaa/testteeth/actions/workflows/ci.yml/badge.svg)](https://github.com/fernandogarzaaa/testteeth/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## The problem

Coding agents (Claude Code, Cursor, Copilot...) are great at producing tests that are green. They are much worse at producing tests that would turn red if the code were wrong. Typical AI-written tests:

- **mirror the implementation** instead of the spec (`assert total(x) == x - x * RATE`), so a wrong formula is "confirmed" by an equally wrong expectation;
- **only walk the happy path**, skipping timeouts, retries, duplicate requests/idempotency, invalid input and partial failures;
- **never touch boundaries** (`>=` vs `>`, `limit` vs `limit + 1`);
- assert that *something* came back (`assert result`, `isinstance(..., float)`) rather than *the right thing*.

Line coverage can't see any of this: the lines run, nothing is checked.

## What testteeth does

testteeth plants small, realistic bugs (**mutants**) in the code you just changed and re-runs your tests against each one. A mutant the tests don't notice (**survived**) is a concrete, provable gap in the suite.

- **Fast and targeted**: git-diff aware. `--base main` mutates only the functions you changed. A one-off line-coverage pass skips running mutants on lines no test executes (reported as `not covered`), and mutants run in parallel in isolated copies of the project.
- **Mutation operators aimed at what agents get wrong**: comparison/boundary flips, `and`/`or`/`not`, arithmetic swaps, off-by-one constants, return value replaced with `None`, **removed `raise`**, **swallowed exceptions** (except-body replaced by `pass`), and **removed call statements** (the retry, `seen.add(key)` dedupe, `save()` or `notify()` call that no test notices is gone).
- **Failure-path coverage check** (heuristic): flags functions with `try/except`, `raise`, retry loops, timeouts or external calls (HTTP, sockets, subprocess, DB, files, `client.*`/`gateway.*` calls) whose error branches no test exercises.
- **Per-function mutation score**, human text and JSON output, and a `--fail-under` gate for CI.
- **pytest plugin** (`pytest --testteeth`) and **CLI** (`testteeth run`).
- **MCP server** so Claude Code / Cursor can call `grade_tests`, `list_surviving_mutants` and `suggest_missing_tests` directly. Each suggestion is an agent-ready brief: the mutated line, why the current tests miss it, and exactly which behaviour or failure path a new test must assert. No LLM calls happen inside testteeth: everything is deterministic and runs locally.

## Quickstart

```bash
pip install "testteeth[mcp] @ git+https://github.com/fernandogarzaaa/testteeth"   # PyPI release: coming soon

cd your-project
testteeth run --base main                 # grade tests for functions changed vs main
testteeth suggest --base main             # agent-ready briefs for every surviving mutant
testteeth run --base origin/main --fail-under 80   # CI gate: exit 1 below 80%
```

Requirements: Python 3.10+, pytest, git (for `--base`). POSIX (Linux/macOS) is the tested platform.

## Example: AI-style tests vs spec-driven tests

[`examples/payments`](examples/payments) is a tiny payments module (pricing with a bulk-discount boundary, an idempotent ledger, a gateway client that retries timeouts). It ships two test suites for the *same* code. Both are 100% green.

```bash
cd examples/payments
testteeth run --pytest-args tests_weak     # typical AI-written tests
testteeth run --pytest-args tests_strong   # spec-driven tests with failure paths
```

| Test suite | Tests | Mutation score | Failure-path gaps |
|---|---|---|---|
| `tests_weak` (AI-style, happy path, mirrors implementation) | 5 | **20.7%** (12/58 mutants killed) | 10 |
| `tests_strong` (spec-driven: boundaries, errors, retries, idempotency) | 29 | **96.6%** (56/58 killed) | 0 |

The two surviving mutants in the strong suite change `round(x, 2)` to `round(x, 3)` on values that are already exact to 2 decimals. They are effectively *equivalent mutants*, which testteeth reports honestly instead of hiding.

Text output for the weak suite (trimmed):

```text
testteeth: mutation grade for all selected code

payments/client.py
  charge_with_retry                    6.2%    1/16  killed  WEAK
      L26   comparison        `attempts < 1` -> `(attempts <= 1)`  [survived]
      L27   remove-raise      `raise ValueError("attempts ...` -> `pass`  [not covered]
      L33   swallow-exception `last_error = exc if attempt...` -> `pass`  [not covered]
      ... 
      ! L18   failure path (timeout): uses a timeout but no test simulates the timeout expiring
      ! L29   failure path (retry): retry loop: no test makes an attempt fail and checks the retry / give-up behaviour
      ! L31   failure path (external-call): calls `gateway.charge` but no test simulates that call failing
      ! L33   failure path (except-branch): `except TimeoutError` branch is never executed by any test
payments/ledger.py
  Ledger.record                       38.5%    5/13  killed  WEAK
      L19   comparison        `amount <= 0` -> `(amount < 0)`  [survived]
      L23   remove-raise      `raise DuplicateAmountMismat...` -> `pass`  [not covered]
      L24   return-value      `return False` -> `return None`  [not covered]
payments/pricing.py
  order_total                         33.3%    4/12  killed  WEAK
      L20   comparison        `total >= BULK_THRESHOLD` -> `(total > BULK_THRESHOLD)`  [survived]
      ...

Mutants: 58  killed: 12  survived: 21  not covered: 25  timeouts: 0
Failure-path gaps: 10
Mutation score: 20.7%
```

`L20 total >= BULK_THRESHOLD -> total > BULK_THRESHOLD [survived]` is exactly the bug class agents ship: the spec says "100.00 or more", and no test ever tries 100.00.

## `testteeth suggest`: briefs for the missing tests

```bash
testteeth suggest --pytest-args tests_weak --format json --limit 2
```

```json
{
  "mutant_id": "payments/client.py#7",
  "function": "charge_with_retry",
  "line": 33,
  "operator": "swallow-exception",
  "status": "no_coverage",
  "original_code": "last_error = exc\nif attempt < attempts - 1:\n    sleep(0.5 * 2**attempt)",
  "mutated_code": "pass",
  "why_missed": "No test executes line 33 inside `except TimeoutError:` at all, so any bug here is invisible. The `except TimeoutError` handler can be replaced by `pass` and nothing fails: no test makes the guarded code raise and checks what the handler does.",
  "must_assert": "first reach this code: call `charge_with_retry` with inputs that take the path inside `except TimeoutError:`; then make the code inside the `try` raise `TimeoutError` (e.g. a fake/mock with `side_effect=TimeoutError(...)`) and assert the handler's observable effect (re-raised error, fallback value, retry, logged/recorded state)",
  "failure_path": true,
  "suggested_test_name": "test_charge_with_retry_swallow_exception_line_33"
}
```

Failure-path briefs come first. The default `--format markdown` renders the same briefs as a document you can paste straight into an agent.

## CLI reference

```text
testteeth run     [PATHS...] [--base REF] [--fail-under N] [--json FILE|-] [options]
testteeth suggest [PATHS...] [--base REF] [--format markdown|json] [--limit N] [options]
testteeth mcp     # start the MCP server on stdio (same as `testteeth-mcp`)

options:
  --root DIR            project root (default: .)
  --pytest-args ARGS    extra pytest args, e.g. "tests/unit -m 'not slow'"
  --workers N           parallel workers (default: min(4, CPUs))
  --max-mutants N       cap mutants (evenly sampled) for a quicker signal
  --operators LIST      comma-separated subset: comparison, boolean, arithmetic, constant,
                        return-value, remove-raise, swallow-exception, remove-call
  --timeout-factor F    mutant timeout = baseline time x F (default 3; a hang counts as killed)
  --no-coverage         don't trace the baseline; run every mutant
```

Exit codes: `0` ok, `1` score below `--fail-under`, `2` configuration error, `3` the suite is red before mutation, a path is missing, or the git ref is unknown.

Defaults can live in `pyproject.toml`:

```toml
[tool.testteeth]
paths = ["src/mypkg"]
test_args = ["tests", "-m", "not slow"]
base_ref = "origin/main"
fail_under = 80
exclude = ["*_pb2.py"]
```

## pytest plugin

Installed automatically with the package:

```bash
pytest --testteeth                                   # grade after a green run
pytest --testteeth --testteeth-base=main --testteeth-fail-under=80
pytest tests/unit --testteeth --testteeth-paths=src/mypkg --testteeth-json=mutation.json
```

The mutant runs reuse your pytest arguments (minus the `--testteeth*` ones). If the normal session fails, grading is skipped. If the score is under the gate, the session exits non-zero and the report is printed in a `testteeth` section of the terminal summary.

## MCP server (Claude Code, Cursor)

Tools:

| Tool | What it returns |
|---|---|
| `grade_tests(path, base_ref?, pytest_args?, paths?, max_mutants?)` | score, per-function scores, counts, failure-path gaps |
| `list_surviving_mutants(path, base_ref?, ..., refresh?)` | the undetected mutants (reuses the last grade with the same arguments) |
| `suggest_missing_tests(path, base_ref?, ..., limit?)` | one brief per surviving mutant + failure-path gaps, as JSON and Markdown |

**Claude Code**:

```bash
claude mcp add testteeth -- testteeth-mcp
```

or in `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "testteeth": { "command": "testteeth-mcp", "args": [] }
  }
}
```

**Cursor**: `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "testteeth": { "command": "testteeth-mcp", "args": [] }
  }
}
```

If `testteeth-mcp` isn't on the PATH the editor uses, point `command` at the full path (for example `.venv/bin/testteeth-mcp`) or use `"command": "python", "args": ["-m", "testteeth", "mcp"]`.

A good agent prompt: *"After changing code, call `grade_tests` with `base_ref: "main"`. For each brief from `suggest_missing_tests`, write a test from the spec that fails on the mutated code and passes on the original. Re-run `grade_tests` until the score is at least 80."*

## How it works

1. **Select**: find `.py` files under the configured paths (skipping tests, venvs, build dirs). With `--base`, `git diff -U0 <ref>` plus untracked files decide which functions changed. Only those are mutated.
2. **Mutate**: walk the AST and record each mutant as an exact replacement of one source span. Formatting and comments elsewhere are untouched, and every mutant is compiled before use. Docstrings, annotations, f-strings, logging/print calls and `if __name__ == "__main__"` are skipped to cut noise.
3. **Baseline**: copy the project to a temp dir and run the suite once with a tiny line tracer. A red suite aborts with its output, since mutation results would be meaningless.
4. **Run**: each covered mutant is written into a worker's copy (with `PYTHONDONTWRITEBYTECODE=1`, so stale bytecode can never mask a mutant). The copy's `src/` and root go first on `PYTHONPATH`, so editable installs of the project don't leak in. Failing tests mean **killed**, green means **survived**, and a hang beyond the timeout counts as **killed (timeout)**.
5. **Analyse**: score per function, failure-path heuristics, and briefs.

Limits worth knowing: it's a heuristic grader, not a proof. Equivalent mutants (changes that can't alter behaviour) show up as survivors. Failure-path detection is pattern based. Each mutant runs the selected test command in a fresh interpreter, so point `--pytest-args` at the fast unit tests for quick feedback.

## Dogfooding: testteeth grades itself

testteeth's own suite has 160 tests, including failure-path tests: red baselines, git errors, unknown refs, hanging mutants, timeouts, stale briefs, invalid config, MCP errors returned as data. CI runs it on Python 3.10–3.12. Running testteeth on its own source with its fast unit tests:

```bash
testteeth run      # uses [tool.testteeth] in this repo's pyproject.toml
```


## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                 # everything (slow end-to-end tests included)
pytest -m "not slow"   # unit tests only
python -m build && twine check dist/*
```

## License

MIT © Fernando Garza
