# Example: ts-payments (TypeScript, graded through StrykerJS)

The TypeScript twin of [`examples/payments`](../payments): the same spec, the same kinds of logic AI-written
tests get wrong, and two green test suites for the *same* code.

- `src/pricing.ts`: a bulk discount that applies from **exactly** 100.00, a promo floor at zero, tiered shipping.
- `src/ledger.ts`: an **idempotent** ledger. Replaying a transaction must not double-count it; replaying it with a
  different amount must throw.
- `src/client.ts`: an async gateway call that **retries timeouts** with exponential backoff, does not retry
  declines, and gives up cleanly.

testteeth drives [StrykerJS](https://stryker-mutator.io) (with the vitest runner) and reads its JSON report.
Each suite has its own vitest config, selected through a small Stryker config file.

```bash
cd examples/ts-payments
npm ci                                                   # installs vitest + StrykerJS locally
testteeth run --engine-config stryker.weak.json          # typical AI-written tests
testteeth suggest --engine-config stryker.weak.json      # vitest-flavoured briefs for the missing tests
testteeth run --engine-config stryker.strong.json --fail-under 95
```

Measured with testteeth 0.2.0, StrykerJS 9.6 and vitest 3.2 (Node 22):

| Suite | Tests | Mutation score | Failure-path gaps (heuristic) |
|---|---|---|---|
| `tests_weak/` (happy path, expectations re-derived from the implementation) | 5 | **32.3%** (32/99 killed: 22 survived, 45 not covered) | 9 |
| `tests_strong/` (spec-driven: exact values, both sides of every boundary, invalid input, replays, timeouts, retry give-up) | 30 | **100%** (99/99 killed) | 0 |

Bugs the weak suite lets through, each reported as a surviving mutant:

- `total >= BULK_THRESHOLD` changed to `total > BULK_THRESHOLD`: a 100.00 order loses its discount.
- The `catch (err)` block emptied: timeouts are swallowed and never retried (`swallow-exception`).
- `throw new DuplicateAmountMismatch(txnId)` removed: a replay with a different amount is silently accepted.
- `amount <= 0` changed to `amount < 0`: zero-amount payments are recorded.
