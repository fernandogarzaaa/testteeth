# Changelog

## 0.3.0 - 2026-11-13

A correctness release from an audit of 0.2.0. If your project uses pytest-cov or pytest-xdist, earlier versions
could report a 0% score for a strong suite; upgrade.

### Changed
- `--base REF` now diffs against the merge base of `REF` and `HEAD` (what a pull request shows) instead of the tip
  of `REF`. Results differ from 0.2.0 when the base branch has moved on since the branch forked.
- `--no-cov` is passed to pytest when pytest-cov is installed (not when it is disabled or plugin autoload is off).
- When the coverage trace records nothing at all, every mutant is run, with a note, instead of being reported
  `not covered`.
- Python client names in the external-call check are matched by whole word.

### Fixed
- **Scores collapsed to 0% next to pytest-cov or pytest-xdist.** With `--cov` in the pytest options, coverage.py
  replaced testteeth's line tracer; with `-n`, the tests ran in worker processes that were never traced. Either
  way every mutant was reported `not covered` and a strong suite scored 0%. The tracer now uses `sys.monitoring`
  on Python 3.12+, xdist workers write their own trace fragments that the engine merges, `--no-cov` is passed to
  the suite when pytest-cov is installed, and a trace that recorded nothing at all makes testteeth run every
  mutant (with a note) instead of guessing.
- **`--base` diffed against the tip of the base ref**, so commits that landed on `main` after the branch forked
  were graded as if the branch had changed them. The diff is now taken from the merge base (falling back to the
  ref when there is none).
- **Rust `--base` graded unchanged neighbours.** Context lines of the diff handed to `cargo mutants --in-diff`
  counted as changed lines, so functions next to the edit received failure-path gaps.
- **TypeScript/Rust function ranges.** A `{` or `;` inside a signature ended or hid the function
  (`function f(opts = {})`, destructured and object-typed parameters, object-literal return types,
  `fn f(salt: [u8; 4])`), so its mutants were attributed to `<module>`, its failure-path gaps were lost and
  `--base` did not select it. An apostrophe in JSX text or a quote in a regex literal swallowed the functions
  that followed. Callbacks (`setTimeout(function () {`) are no longer named after their callee, and
  `export default function () {` is recognised.
- **TypeScript failure paths.** A `throw` on the same line as its covered guard (`if (x <= 0) throw ...`) and a
  `catch` block whose only StrykerJS verdict is "no coverage" on the `catch (e) {` line were not reported.
- **Rust failure paths.** `Err(..)` in patterns and comparisons (`matches!(r, Err(_))`, `r == Err(..)`,
  `Some(Err(_)) =>`, `Err(e) if e.code == 2 =>`) was counted as an error return.
- **Mutant spans shifted by unusual characters.** Lines were split with `str.splitlines()`, which also breaks on
  form feeds, U+2028 and a few other characters that parsers do not treat as line breaks, so every mutant after
  one had the wrong text. Lines are now split by each language's own rules (Python, ECMAScript, Rust).
- **StrykerJS columns are UTF-16 code units.** After an emoji (or any astral character) on the same line the
  mutant's original text and brief were off by one per character.
- **Nested template literals** (`` `${xs.map(x => `{${x}`)}` ``) unbalanced the TypeScript scanner's braces;
  `${...}` expressions are now scanned as code. Generic arrow functions (`async <T,>(a: T) => {`) are detected.
- **Python external-call check matched substrings**: `capitals.get()` and `feedback.get()` counted as client
  calls because they contain "api" and "db". Whole words are matched now (`api_client`, `apiClient`, `self.db`).
- TypeScript briefs for an emptied `catch` block used Python wording (`except`, `pass`).
- Non-ASCII file names were dropped from `--base` selection (git quotes them by default).
- Failure injected through a test decorator (`@patch(..., side_effect=TimeoutError)`) was not seen by the Python
  failure-path check, which reported a false gap.
- Wrongly typed settings (`workers = "4"`, `fail_under = true`) crashed with a `TypeError`; they are now
  configuration errors (exit code 2).
- `testteeth.toml` was ignored when a `pyproject.toml` without `[tool.testteeth]` existed.
- `pytest --testteeth -n N` no longer attempts to grade inside xdist workers, and `--collect-only` skips grading.
- `testteeth suggest` and the MCP `suggest_missing_tests` tool no longer crash when a mutated file was deleted
  or is not UTF-8; the MCP result cache now distinguishes `max_mutants`.
- `testteeth suggest` and the MCP tool name the files whose surviving mutants got no brief (missing or not
  UTF-8) instead of reporting a clean result.
- CI checkouts no longer persist the GitHub token (`persist-credentials: false`).

### Added
- `# pragma: no mutate` in a comment on a Python line excludes every mutant whose replaced span touches that
  line (for reviewed equivalent mutants).
- Python 3.13 and 3.14 are tested in CI; ruff and mypy configurations are part of the project and enforced in CI.

## 0.2.0 - 2026-10-08

Multi-language support: testteeth now grades **TypeScript/JavaScript** (via StrykerJS) and **Rust** (via
cargo-mutants) as well as Python, with the same report, failure-path checks and agent briefs.

### Added
- **Language-agnostic core.** A shared report model (mutant id, file, line, function, operator, original and
  mutated code, status killed / survived / not covered / timeout, plus the engine's own mutator name) consumed by
  scoring, `--fail-under`, diff selection, failure-path gaps, text/JSON output and MCP briefs. Python keeps its
  own engine as one adapter.
- **TypeScript/JavaScript adapter (StrykerJS).** Generates a Stryker config whose `mutate` is limited to the
  selected files, or with `--base` to the line ranges of the changed functions; runs `npx stryker run`; reads the
  mutation-testing-report-schema JSON. Merges your JSON Stryker config (or `--engine-config`), passes JS configs
  through with CLI overrides, and picks the test runner from the installed `@stryker-mutator/*-runner` plugin.
- **Rust adapter (cargo-mutants).** Runs `cargo mutants` with `--in-diff` (a diff of changed `.rs` files,
  untracked files included) for `--base` and `--file` filters for explicit paths; reads
  `mutants.out/outcomes.json`. `--engine-args "-- --test NAME"` selects the cargo test target.
- **Language detection** from `pyproject.toml` / `setup.py`, `package.json` and `Cargo.toml` (source-file count
  breaks ties, with a note), overridable with `--lang` (`python`, `typescript`/`ts`/`javascript`/`js`,
  `rust`/`rs`). New options `--engine-args` and `--engine-config`; settings can live in `testteeth.toml` for
  non-Python projects.
- **Failure-path rule packs (heuristic).** TypeScript/JavaScript: `catch` blocks never executed (using
  StrykerJS coverage), `throw` never triggered, Promise `.catch`, `fetch`/axios/HTTP-client calls,
  `setTimeout`/`AbortController` timeouts, retry loops. Rust: `Err(..)` returns, `?` propagation,
  `match`/`if let` `Err` arms, `unwrap()`/`expect()`, timeouts, reqwest/tokio/std I/O calls, retry loops.
- **Ecosystem-specific briefs.** TypeScript briefs suggest vitest/jest idioms (`vi.fn().mockRejectedValueOnce`,
  `rejects.toThrow`, fake timers); Rust briefs suggest `#[test]` / `#[tokio::test]`, `assert!(matches!(.., Err(..)))`
  and mockall. Both include a test skeleton (`test_skeleton` in JSON).
- **MCP tools accept an optional `lang`** (and `engine_args`); engine problems are returned as data with an
  `install_hint`.
- **Clear failures** for a missing engine (exit 3 with an install hint), an engine crash, a red baseline, a
  malformed engine report and an unknown `--lang` (exit 2).
- **Examples** `examples/ts-payments` and `examples/rust-payments` mirroring the Python payments demo, with
  measured scores: TypeScript weak 32.3% vs strong 100%; Rust weak 50.0% vs strong 100%.
- Tests for each adapter driven by recorded StrykerJS and cargo-mutants reports, the rule packs, language
  detection and the failure paths above (298 tests). CI gains a job that installs StrykerJS and cargo-mutants
  and grades both examples end-to-end.

### Changed
- JSON reports gain `lang`, `engine` and `excluded` (mutants the engine could not evaluate); failure-path gaps
  gain `lang`, gap briefs gain `heuristic: true`, and test briefs gain `lang` and `test_skeleton`. Existing keys
  and Python behaviour are unchanged.

## 0.1.0 - 2026-10-08

- First release: git-diff-aware mutation testing for Python with operators aimed at what AI-written tests miss,
  failure-path heuristics, per-function scores, text/JSON output, `--fail-under`, a pytest plugin, and an MCP
  server (`grade_tests`, `list_surviving_mutants`, `suggest_missing_tests`) with agent-ready briefs.
