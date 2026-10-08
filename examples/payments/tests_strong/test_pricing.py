"""Spec-driven tests: exact values from the spec, boundaries on both sides, invalid input."""

import pytest

from payments.pricing import order_total, shipping_fee


@pytest.mark.parametrize(
    ("subtotal", "expected"),
    [(99.99, 99.99), (100.0, 90.0), (100.01, 90.01), (250.0, 225.0), (0.0, 0.0)],
)
def test_bulk_discount_applies_from_exactly_100(subtotal, expected):
    assert order_total(subtotal) == expected


def test_promo_code_takes_five_off():
    assert order_total(20.0, "WELCOME5") == 15.0


def test_promo_applies_after_bulk_discount():
    assert order_total(200.0, "WELCOME5") == 175.0


def test_promo_never_goes_below_zero():
    assert order_total(3.0, "WELCOME5") == 0.0


def test_unknown_promo_is_ignored():
    assert order_total(20.0, "welcome5") == 20.0


def test_negative_subtotal_is_rejected():
    with pytest.raises(ValueError, match="negative"):
        order_total(-0.01)


@pytest.mark.parametrize(
    ("subtotal", "weight", "expected"),
    [
        (49.99, 1.0, 4.99),  # just under the free-shipping line
        (50.0, 30.0, 0.0),  # free from exactly 50
        (10.0, 2.0, 4.99),  # first 2 kg included
        (10.0, 3.0, 6.49),  # 1 kg extra
        (10.0, 6.0, 10.99),  # 4 kg extra
    ],
)
def test_shipping_fee(subtotal, weight, expected):
    assert shipping_fee(subtotal, weight) == expected
