//! Charging a card through a flaky payment gateway, with retries on timeouts.

use std::time::Duration;

#[derive(Debug, Clone, PartialEq)]
pub enum GatewayError {
    Timeout,
    Declined(String),
}

#[derive(Debug, PartialEq)]
pub enum PaymentError {
    InvalidAttempts,
    Declined(String),
    /// The charge did not go through after all attempts.
    GaveUp { txn_id: String, attempts: u32 },
}

pub trait Gateway {
    fn charge(&mut self, txn_id: &str, amount: f64, timeout: Duration) -> Result<String, GatewayError>;
}

/// Charge `amount`; retry timeouts with exponential backoff (0.5s, 1s, ...); declines are not retried.
pub fn charge_with_retry<G: Gateway, S: FnMut(Duration)>(
    gateway: &mut G,
    txn_id: &str,
    amount: f64,
    attempts: u32,
    mut sleep: S,
) -> Result<String, PaymentError> {
    if attempts < 1 {
        return Err(PaymentError::InvalidAttempts);
    }
    for attempt in 0..attempts {
        match gateway.charge(txn_id, amount, Duration::from_secs(5)) {
            Ok(charge_id) => return Ok(charge_id),
            Err(GatewayError::Declined(reason)) => return Err(PaymentError::Declined(reason)),
            Err(GatewayError::Timeout) => {
                if attempt < attempts - 1 {
                    sleep(Duration::from_millis(500 * 2u64.pow(attempt)));
                }
            }
        }
    }
    Err(PaymentError::GaveUp { txn_id: txn_id.to_string(), attempts })
}
