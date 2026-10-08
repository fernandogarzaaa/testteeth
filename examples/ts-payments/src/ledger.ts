/** An idempotent payment ledger: recording the same transaction twice must not double-count it. */

export class DuplicateAmountMismatch extends Error {
  constructor(txnId: string) {
    super(`transaction ${txnId} replayed with a different amount`);
    this.name = "DuplicateAmountMismatch";
  }
}

export class Ledger {
  private amounts = new Map<string, number>();
  readonly entries: Array<[string, number]> = [];

  /** Record a payment. Returns false (and changes nothing) for a replay of the same transaction. */
  record(txnId: string, amount: number): boolean {
    if (!txnId) {
      throw new Error("txnId is required");
    }
    if (amount <= 0) {
      throw new RangeError("amount must be positive");
    }
    const previous = this.amounts.get(txnId);
    if (previous !== undefined) {
      if (previous !== amount) {
        throw new DuplicateAmountMismatch(txnId);
      }
      return false;
    }
    this.amounts.set(txnId, amount);
    this.entries.push([txnId, amount]);
    return true;
  }

  balance(): number {
    const sum = this.entries.reduce((acc, [, amount]) => acc + amount, 0);
    return Math.round(sum * 100) / 100;
  }
}
