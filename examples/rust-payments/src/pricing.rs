//! Pricing rules.
//!
//! Spec:
//! * Orders of 100.00 or more get a 10% bulk discount.
//! * Promo code "WELCOME5" takes 5.00 off (never below zero).
//! * Shipping is free from 50.00; otherwise 4.99 plus 1.50 per kg above the first 2 kg.

pub const BULK_THRESHOLD: f64 = 100.0;
pub const BULK_RATE: f64 = 0.10;
pub const FREE_SHIPPING_FROM: f64 = 50.0;

#[derive(Debug, PartialEq)]
pub enum PricingError {
    NegativeSubtotal,
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

pub fn order_total(subtotal: f64, promo: Option<&str>) -> Result<f64, PricingError> {
    if subtotal < 0.0 {
        return Err(PricingError::NegativeSubtotal);
    }
    let mut total = subtotal;
    if total >= BULK_THRESHOLD {
        total = total - total * BULK_RATE;
    }
    if promo == Some("WELCOME5") {
        total = (total - 5.0).max(0.0);
    }
    Ok(round2(total))
}

pub fn shipping_fee(subtotal: f64, weight_kg: f64) -> f64 {
    if subtotal >= FREE_SHIPPING_FROM {
        return 0.0;
    }
    let extra_kg = (weight_kg - 2.0).max(0.0);
    round2(4.99 + 1.5 * extra_kg)
}
