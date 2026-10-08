/**
 * Pricing rules.
 *
 * Spec:
 * - Orders of 100.00 or more get a 10% bulk discount.
 * - Promo code "WELCOME5" takes 5.00 off (never below zero).
 * - Shipping is free from 50.00; otherwise 4.99 plus 1.50 per kg above the first 2 kg.
 */

export const BULK_THRESHOLD = 100;
export const BULK_RATE = 0.1;
export const FREE_SHIPPING_FROM = 50;

const round2 = (x: number): number => Math.round(x * 100) / 100;

export function orderTotal(subtotal: number, promo?: string): number {
  if (subtotal < 0) {
    throw new RangeError("subtotal cannot be negative");
  }
  let total = subtotal;
  if (total >= BULK_THRESHOLD) {
    total = total - total * BULK_RATE;
  }
  if (promo === "WELCOME5") {
    total = Math.max(total - 5, 0);
  }
  return round2(total);
}

export function shippingFee(subtotal: number, weightKg: number): number {
  if (subtotal >= FREE_SHIPPING_FROM) {
    return 0;
  }
  const extraKg = Math.max(weightKg - 2, 0);
  return round2(4.99 + 1.5 * extraKg);
}
