// Spec-driven ledger tests: idempotency, replay with a different amount, invalid input.
import { describe, expect, it } from "vitest";
import { DuplicateAmountMismatch, Ledger } from "../src/ledger";

describe("Ledger", () => {
  it("records a new transaction", () => {
    const ledger = new Ledger();
    expect(ledger.record("t1", 10)).toBe(true);
    expect(ledger.entries).toEqual([["t1", 10]]);
    expect(ledger.balance()).toBe(10);
  });

  it("a replay is a no-op", () => {
    const ledger = new Ledger();
    ledger.record("t1", 10);
    expect(ledger.record("t1", 10)).toBe(false);
    expect(ledger.entries).toHaveLength(1);
    expect(ledger.balance()).toBe(10);
  });

  it("a replay with a different amount is rejected", () => {
    const ledger = new Ledger();
    ledger.record("t1", 10);
    expect(() => ledger.record("t1", 12)).toThrow(DuplicateAmountMismatch);
    expect(() => ledger.record("t1", 12)).toThrow("transaction t1 replayed with a different amount");
    expect(ledger.balance()).toBe(10);
  });

  it("an empty txnId is rejected", () => {
    const ledger = new Ledger();
    expect(() => ledger.record("", 10)).toThrow("txnId is required");
    expect(ledger.entries).toEqual([]);
  });

  it("zero and negative amounts are rejected", () => {
    const ledger = new Ledger();
    expect(() => ledger.record("t1", 0)).toThrow("amount must be positive");
    expect(() => ledger.record("t1", -1)).toThrow(RangeError);
  });

  it("balance sums distinct transactions", () => {
    const ledger = new Ledger();
    ledger.record("t1", 10.1);
    ledger.record("t2", 0.2);
    expect(ledger.balance()).toBe(10.3);
  });

  it("an empty ledger has a zero balance", () => expect(new Ledger().balance()).toBe(0));

  it("errors carry their name", () => expect(new DuplicateAmountMismatch("x").name).toBe("DuplicateAmountMismatch"));
});
