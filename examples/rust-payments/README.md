# Example: rust-payments (Rust, graded through cargo-mutants)

The Rust twin of [`examples/payments`](../payments): the same spec and two green test suites for the *same*
crate, as integration-test targets `tests/weak.rs` and `tests/strong.rs`.

- `src/pricing.rs`: a bulk discount that applies from **exactly** 100.00, a promo floor at zero, tiered shipping;
  a negative subtotal is an `Err`.
- `src/ledger.rs`: an **idempotent** ledger. A replay returns `Ok(false)`; a replay with a different amount is
  `Err(DuplicateAmountMismatch)`.
- `src/client.rs`: a gateway call (behind a `Gateway` trait) that **retries timeouts** with exponential backoff,
  does not retry declines, and gives up with `Err(GaveUp)`.

testteeth drives [cargo-mutants](https://mutants.rs) and reads `mutants.out/outcomes.json`. Arguments after
`--` in `--engine-args` go to `cargo test`, which is how each suite is selected.

```bash
cd examples/rust-payments
cargo install --locked cargo-mutants                       # once
testteeth run --engine-args "-- --test weak"               # typical AI-written tests
testteeth suggest --engine-args "-- --test weak"           # #[test]-flavoured briefs for the missing tests
testteeth run --engine-args "-- --test strong" --fail-under 95
```

Measured with testteeth 0.2.0, cargo-mutants 27.1 and Rust 1.99:

| Suite | Tests | Mutation score | Failure-path gaps (heuristic) |
|---|---|---|---|
| `tests/weak.rs` (happy path, `is_ok()` checks, expectations re-derived from the implementation) | 5 | **50.0%** (27/54 killed) | 7 |
| `tests/strong.rs` (spec-driven: exact values and errors, boundaries, replays, timeouts, retry give-up) | 28 | **100%** (54/54 killed) | 0 |

Scores are not comparable across languages: each engine has its own mutators. cargo-mutants replaces whole
function bodies with plausible values (`Ok(Default::default())`, `0.0`, `1.0`...) and swaps binary operators, but
it has no string-literal mutants and turns `>=` into `<` rather than `>`, so it plants fewer, different bugs than
StrykerJS or testteeth's Python engine. The failure-path gaps are what the weak suite misses most: no test
asserts an `Err`, makes the gateway time out, or checks the retry loop.
