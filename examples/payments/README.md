# Example: payments

A deliberately small module with the kinds of logic AI-written tests get wrong:

- `payments/pricing.py`: a bulk discount that applies from **exactly** 100.00, a promo floor at zero, tiered shipping.
- `payments/ledger.py`: an **idempotent** ledger. Replaying a transaction must not double-count it, and replaying it with a different amount must be rejected.
- `payments/client.py`: a gateway call that **retries timeouts** with exponential backoff, does not retry declines, and gives up cleanly.

The code is correct, and both suites below are green. The difference is how many *bugs* each suite would catch. testteeth measures that by planting them (mutants).

| Suite | Style | Mutation score |
|---|---|---|
| `tests_weak/` | What an agent typically writes: happy path only, expectations re-derived from the implementation (`subtotal - subtotal * BULK_RATE`), `isinstance` and `> 0` checks | **20.7%** |
| `tests_strong/` | Spec-driven: exact values, both sides of every boundary, invalid input, duplicate requests, timeouts and retry give-up | **96.6%** |

```bash
cd examples/payments
testteeth run --pytest-args tests_weak
testteeth suggest --pytest-args tests_weak     # briefs for the missing tests
testteeth run --pytest-args tests_strong --fail-under 90
```

Bugs the weak suite lets through, each reported as a surviving mutant:

- `total >= BULK_THRESHOLD` changed to `>`: a 100.00 order loses its discount.
- `raise DuplicateAmountMismatch` deleted: a replay with a different amount is silently accepted.
- The `except TimeoutError` body replaced with `pass`: retries and backoff are silently skipped.
- `amount <= 0` changed to `< 0`: zero-amount payments are recorded.
