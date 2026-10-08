//! Typical AI-generated tests: happy path only, expectations re-derived from the implementation.

use payments::client::{charge_with_retry, Gateway, GatewayError};
use payments::ledger::Ledger;
use payments::pricing::{order_total, shipping_fee, BULK_RATE, BULK_THRESHOLD};
use std::time::Duration;

#[test]
fn order_total_is_ok() {
    assert!(order_total(20.0, None).is_ok());
}

#[test]
fn order_total_with_bulk_discount() {
    let subtotal = 250.0;
    // mirrors the implementation instead of stating the expected number
    let expected = ((subtotal - subtotal * BULK_RATE) * 100.0_f64).round() / 100.0;
    assert_eq!(order_total(subtotal, None).unwrap(), expected);
    assert!(subtotal > BULK_THRESHOLD);
}

#[test]
fn shipping_fee_is_positive() {
    assert!(shipping_fee(10.0, 1.0) > 0.0);
}

#[test]
fn ledger_record() {
    let mut ledger = Ledger::new();
    assert!(ledger.record("t1", 10.0).is_ok());
    assert_eq!(ledger.entries.len(), 1);
}

struct OkGateway;

impl Gateway for OkGateway {
    fn charge(&mut self, _txn_id: &str, _amount: f64, _timeout: Duration) -> Result<String, GatewayError> {
        Ok("ch_123".to_string())
    }
}

#[test]
fn charge_with_retry_success() {
    let result = charge_with_retry(&mut OkGateway, "t1", 10.0, 3, |_| {});
    assert_eq!(result.unwrap(), "ch_123");
}
