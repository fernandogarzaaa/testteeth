// Spec-driven pricing tests: exact values on both sides of every boundary.
import { describe, expect, it } from "vitest";
import { orderTotal, shippingFee } from "../src/pricing";

describe("orderTotal", () => {
  it("no discount just below the threshold", () => expect(orderTotal(99.99)).toBe(99.99));
  it("discount applies at exactly 100", () => expect(orderTotal(100)).toBe(90));
  it("discount above the threshold", () => expect(orderTotal(250)).toBe(225));
  it("promo takes five off", () => expect(orderTotal(20, "WELCOME5")).toBe(15));
  it("promo applies after the discount", () => expect(orderTotal(200, "WELCOME5")).toBe(175));
  it("promo never goes below zero", () => expect(orderTotal(3, "WELCOME5")).toBe(0));
  it("unknown promo is ignored", () => expect(orderTotal(20, "welcome5")).toBe(20));
  it("zero subtotal is allowed", () => expect(orderTotal(0)).toBe(0));
  it("negative subtotal is rejected", () => {
    expect(() => orderTotal(-0.01)).toThrow(RangeError);
    expect(() => orderTotal(-0.01)).toThrow("subtotal cannot be negative");
  });
  it("rounds to cents", () => {
    expect(orderTotal(10.004)).toBe(10);
    expect(orderTotal(10.006)).toBe(10.01);
  });
});

describe("shippingFee", () => {
  it("free from exactly 50", () => expect(shippingFee(50, 10)).toBe(0));
  it("base fee below 50", () => expect(shippingFee(49.99, 1)).toBe(4.99));
  it("first two kg included", () => expect(shippingFee(10, 2)).toBe(4.99));
  it("per kg above two", () => {
    expect(shippingFee(10, 4)).toBe(7.99);
    expect(shippingFee(10, 3)).toBe(6.49);
  });
});
