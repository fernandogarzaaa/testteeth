# Changelog

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
