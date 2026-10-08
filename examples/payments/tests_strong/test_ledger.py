"""Ledger behaviour, including idempotency and invalid input."""

import pytest

from payments.ledger import DuplicateAmountMismatch, Ledger


def test_record_returns_true_and_updates_balance():
    ledger = Ledger()
    assert ledger.record("t1", 10.0) is True
    assert ledger.record("t2", 2.5) is True
    assert ledger.entries == [("t1", 10.0), ("t2", 2.5)]
    assert ledger.balance() == 12.5


def test_replaying_a_transaction_is_a_noop():
    ledger = Ledger()
    ledger.record("t1", 10.0)
    assert ledger.record("t1", 10.0) is False
    assert ledger.balance() == 10.0
    assert len(ledger.entries) == 1


def test_replay_with_different_amount_is_rejected_without_side_effects():
    ledger = Ledger()
    ledger.record("t1", 10.0)
    with pytest.raises(DuplicateAmountMismatch):
        ledger.record("t1", 11.0)
    assert ledger.balance() == 10.0


@pytest.mark.parametrize(("txn_id", "amount"), [("", 5.0), ("t1", 0.0), ("t1", -1.0)])
def test_invalid_input_is_rejected(txn_id, amount):
    ledger = Ledger()
    with pytest.raises(ValueError):
        ledger.record(txn_id, amount)
    assert ledger.entries == []


def test_smallest_positive_amount_is_accepted():
    assert Ledger().record("t1", 0.01) is True


def test_empty_balance_is_zero():
    assert Ledger().balance() == 0
