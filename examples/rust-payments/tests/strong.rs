//! Spec-driven tests: exact values, both sides of every boundary, invalid input,
//! duplicate requests, timeouts and retry give-up.

use payments::client::{charge_with_retry, Gateway, GatewayError, PaymentError};
use payments::ledger::{Ledger, LedgerError};
use payments::pricing::{order_total, shipping_fee, PricingError};
use std::collections::VecDeque;
use std::time::Duration;

// ---------------------------------------------------------------- pricing
#[test]
fn no_discount_just_below_threshold() {
    assert_eq!(order_total(99.99, None), Ok(99.99));
}

#[test]
fn discount_applies_at_exactly_100() {
    assert_eq!(order_total(100.0, None), Ok(90.0));
}

#[test]
fn discount_above_threshold() {
    assert_eq!(order_total(250.0, None), Ok(225.0));
}

#[test]
fn promo_takes_five_off() {
    assert_eq!(order_total(20.0, Some("WELCOME5")), Ok(15.0));
}

#[test]
fn promo_applies_after_discount() {
    assert_eq!(order_total(200.0, Some("WELCOME5")), Ok(175.0));
}

#[test]
fn promo_never_goes_below_zero() {
    assert_eq!(order_total(3.0, Some("WELCOME5")), Ok(0.0));
}

#[test]
fn unknown_promo_is_ignored() {
    assert_eq!(order_total(20.0, Some("welcome5")), Ok(20.0));
}

#[test]
fn zero_subtotal_is_allowed() {
    assert_eq!(order_total(0.0, None), Ok(0.0));
}

#[test]
fn negative_subtotal_is_rejected() {
    assert_eq!(order_total(-0.01, None), Err(PricingError::NegativeSubtotal));
}

#[test]
fn total_is_rounded_to_cents() {
    assert_eq!(order_total(10.004, None), Ok(10.0));
    assert_eq!(order_total(10.006, None), Ok(10.01));
}

#[test]
fn shipping_free_from_exactly_50() {
    assert_eq!(shipping_fee(50.0, 10.0), 0.0);
}

#[test]
fn shipping_base_fee_below_50() {
    assert_eq!(shipping_fee(49.99, 1.0), 4.99);
}

#[test]
fn shipping_first_two_kg_included() {
    assert_eq!(shipping_fee(10.0, 2.0), 4.99);
}

#[test]
fn shipping_per_kg_above_two() {
    assert_eq!(shipping_fee(10.0, 4.0), 7.99);
    assert_eq!(shipping_fee(10.0, 3.0), 6.49);
}

// ---------------------------------------------------------------- ledger
#[test]
fn record_new_transaction() {
    let mut ledger = Ledger::new();
    assert_eq!(ledger.record("t1", 10.0), Ok(true));
    assert_eq!(ledger.entries, vec![("t1".to_string(), 10.0)]);
    assert_eq!(ledger.balance(), 10.0);
}

#[test]
fn replay_is_a_no_op() {
    let mut ledger = Ledger::new();
    ledger.record("t1", 10.0).unwrap();
    assert_eq!(ledger.record("t1", 10.0), Ok(false));
    assert_eq!(ledger.entries.len(), 1);
    assert_eq!(ledger.balance(), 10.0);
}

#[test]
fn replay_with_different_amount_is_rejected() {
    let mut ledger = Ledger::new();
    ledger.record("t1", 10.0).unwrap();
    assert_eq!(
        ledger.record("t1", 12.0),
        Err(LedgerError::DuplicateAmountMismatch("t1".to_string()))
    );
    assert_eq!(ledger.balance(), 10.0);
}

#[test]
fn empty_txn_id_is_rejected() {
    let mut ledger = Ledger::new();
    assert_eq!(ledger.record("", 10.0), Err(LedgerError::MissingTxnId));
    assert!(ledger.entries.is_empty());
}

#[test]
fn zero_amount_is_rejected() {
    let mut ledger = Ledger::new();
    assert_eq!(ledger.record("t1", 0.0), Err(LedgerError::NonPositiveAmount));
}

#[test]
fn negative_amount_is_rejected() {
    let mut ledger = Ledger::new();
    assert_eq!(ledger.record("t1", -1.0), Err(LedgerError::NonPositiveAmount));
}

#[test]
fn balance_sums_distinct_transactions() {
    let mut ledger = Ledger::new();
    ledger.record("t1", 10.10).unwrap();
    ledger.record("t2", 0.20).unwrap();
    assert_eq!(ledger.balance(), 10.3);
}

#[test]
fn balance_of_empty_ledger_is_zero() {
    assert_eq!(Ledger::new().balance(), 0.0);
}

// ---------------------------------------------------------------- client
struct FakeGateway {
    outcomes: VecDeque<Result<String, GatewayError>>,
    calls: Vec<(String, f64, Duration)>,
}

impl FakeGateway {
    fn new(outcomes: Vec<Result<String, GatewayError>>) -> Self {
        Self { outcomes: outcomes.into(), calls: Vec::new() }
    }
}

impl Gateway for FakeGateway {
    fn charge(&mut self, txn_id: &str, amount: f64, timeout: Duration) -> Result<String, GatewayError> {
        self.calls.push((txn_id.to_string(), amount, timeout));
        self.outcomes.pop_front().expect("unexpected extra call")
    }
}

#[test]
fn success_on_first_attempt_does_not_sleep() {
    let mut gateway = FakeGateway::new(vec![Ok("ch_1".into())]);
    let mut sleeps = Vec::new();
    let result = charge_with_retry(&mut gateway, "t1", 9.5, 3, |d| sleeps.push(d));
    assert_eq!(result, Ok("ch_1".to_string()));
    assert_eq!(gateway.calls, vec![("t1".to_string(), 9.5, Duration::from_secs(5))]);
    assert!(sleeps.is_empty());
}

#[test]
fn timeouts_are_retried_with_exponential_backoff() {
    let mut gateway = FakeGateway::new(vec![
        Err(GatewayError::Timeout),
        Err(GatewayError::Timeout),
        Ok("ch_3".into()),
    ]);
    let mut sleeps = Vec::new();
    let result = charge_with_retry(&mut gateway, "t1", 9.5, 3, |d| sleeps.push(d));
    assert_eq!(result, Ok("ch_3".to_string()));
    assert_eq!(gateway.calls.len(), 3);
    assert_eq!(sleeps, vec![Duration::from_millis(500), Duration::from_millis(1000)]);
}

#[test]
fn gives_up_after_all_attempts_without_a_final_sleep() {
    let mut gateway = FakeGateway::new(vec![Err(GatewayError::Timeout); 3]);
    let mut sleeps = Vec::new();
    let result = charge_with_retry(&mut gateway, "t1", 9.5, 3, |d| sleeps.push(d));
    assert_eq!(result, Err(PaymentError::GaveUp { txn_id: "t1".to_string(), attempts: 3 }));
    assert_eq!(gateway.calls.len(), 3);
    assert_eq!(sleeps, vec![Duration::from_millis(500), Duration::from_millis(1000)]);
}

#[test]
fn single_attempt_means_no_retry() {
    let mut gateway = FakeGateway::new(vec![Err(GatewayError::Timeout)]);
    let mut sleeps = Vec::new();
    let result = charge_with_retry(&mut gateway, "t1", 9.5, 1, |d| sleeps.push(d));
    assert!(matches!(result, Err(PaymentError::GaveUp { attempts: 1, .. })));
    assert_eq!(gateway.calls.len(), 1);
    assert!(sleeps.is_empty());
}

#[test]
fn declines_are_not_retried() {
    let mut gateway = FakeGateway::new(vec![Err(GatewayError::Declined("card declined".into())), Ok("never".into())]);
    let result = charge_with_retry(&mut gateway, "t1", 9.5, 3, |_| {});
    assert_eq!(result, Err(PaymentError::Declined("card declined".to_string())));
    assert_eq!(gateway.calls.len(), 1);
}

#[test]
fn attempts_must_be_positive() {
    let mut gateway = FakeGateway::new(vec![]);
    let result = charge_with_retry(&mut gateway, "t1", 9.5, 0, |_| {});
    assert_eq!(result, Err(PaymentError::InvalidAttempts));
    assert!(gateway.calls.is_empty());
}
