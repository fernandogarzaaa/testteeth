"""Typical AI-generated tests: happy path only, expectations re-derived from the implementation."""

from payments.client import charge_with_retry
from payments.ledger import Ledger
from payments.pricing import BULK_RATE, BULK_THRESHOLD, order_total, shipping_fee


def test_order_total_returns_float():
    assert isinstance(order_total(20.0), float)


def test_order_total_with_bulk_discount():
    subtotal = 250.0
    # mirrors the implementation instead of stating the expected number
    assert order_total(subtotal) == round(subtotal - subtotal * BULK_RATE, 2)
    assert subtotal > BULK_THRESHOLD


def test_shipping_fee_is_positive():
    assert shipping_fee(10.0, 1.0) > 0


def test_ledger_record():
    ledger = Ledger()
    assert ledger.record("t1", 10.0)
    assert len(ledger.entries) == 1


def test_charge_with_retry_success():
    class OkGateway:
        def charge(self, txn_id, amount, timeout):
            return "ch_123"

    assert charge_with_retry(OkGateway(), "t1", 10.0, sleep=lambda s: None) == "ch_123"
