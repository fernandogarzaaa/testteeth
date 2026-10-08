//! An idempotent payment ledger: recording the same transaction twice must not double-count it.

use std::collections::HashMap;

#[derive(Debug, PartialEq)]
pub enum LedgerError {
    MissingTxnId,
    NonPositiveAmount,
    /// The same transaction id was replayed with a different amount.
    DuplicateAmountMismatch(String),
}

#[derive(Default)]
pub struct Ledger {
    amounts: HashMap<String, f64>,
    pub entries: Vec<(String, f64)>,
}

impl Ledger {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record a payment. Returns Ok(false) (and changes nothing) for a replay of the same transaction.
    pub fn record(&mut self, txn_id: &str, amount: f64) -> Result<bool, LedgerError> {
        if txn_id.is_empty() {
            return Err(LedgerError::MissingTxnId);
        }
        if amount <= 0.0 {
            return Err(LedgerError::NonPositiveAmount);
        }
        if let Some(previous) = self.amounts.get(txn_id) {
            if *previous != amount {
                return Err(LedgerError::DuplicateAmountMismatch(txn_id.to_string()));
            }
            return Ok(false);
        }
        self.amounts.insert(txn_id.to_string(), amount);
        self.entries.push((txn_id.to_string(), amount));
        Ok(true)
    }

    pub fn balance(&self) -> f64 {
        let sum: f64 = self.entries.iter().map(|(_, amount)| amount).sum();
        (sum * 100.0).round() / 100.0
    }
}
