// Typical AI-generated tests: happy path only, expectations re-derived from the implementation.
import { describe, expect, it } from "vitest";
import { chargeWithRetry } from "../src/client";
import { Ledger } from "../src/ledger";
import { BULK_RATE, BULK_THRESHOLD, orderTotal, shippingFee } from "../src/pricing";

describe("payments", () => {
  it("orderTotal returns a number", () => {
    expect(typeof orderTotal(20)).toBe("number");
  });

  it("orderTotal applies the bulk discount", () => {
    const subtotal = 250;
    // mirrors the implementation instead of stating the expected number
    expect(orderTotal(subtotal)).toBe(Math.round((subtotal - subtotal * BULK_RATE) * 100) / 100);
    expect(subtotal).toBeGreaterThan(BULK_THRESHOLD);
  });

  it("shippingFee is positive", () => {
    expect(shippingFee(10, 1)).toBeGreaterThan(0);
  });

  it("ledger records", () => {
    const ledger = new Ledger();
    expect(ledger.record("t1", 10)).toBeTruthy();
    expect(ledger.entries.length).toBe(1);
  });

  it("chargeWithRetry succeeds", async () => {
    const gateway = { charge: async () => "ch_123" };
    expect(await chargeWithRetry(gateway, "t1", 10, 3, async () => {})).toBe("ch_123");
  });
});
