"""Pricing rules.

Spec:
* Orders of 100.00 or more get a 10% bulk discount.
* Promo code "WELCOME5" takes 5.00 off (never below zero).
* Shipping is free from 50.00; otherwise 4.99 plus 1.50 per kg above the first 2 kg.
"""

from __future__ import annotations

BULK_THRESHOLD = 100.0
BULK_RATE = 0.10
FREE_SHIPPING_FROM = 50.0


def order_total(subtotal: float, promo: str | None = None) -> float:
    if subtotal < 0:
        raise ValueError("subtotal cannot be negative")
    total = subtotal
    if total >= BULK_THRESHOLD:
        total = total - total * BULK_RATE
    if promo == "WELCOME5":
        total = max(total - 5.0, 0.0)
    return round(total, 2)


def shipping_fee(subtotal: float, weight_kg: float) -> float:
    if subtotal >= FREE_SHIPPING_FROM:
        return 0.0
    extra_kg = max(weight_kg - 2, 0)
    return round(4.99 + 1.5 * extra_kg, 2)
