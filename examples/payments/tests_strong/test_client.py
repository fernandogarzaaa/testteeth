"""Retry behaviour: timeouts are retried with backoff, declines are not, and we give up cleanly."""

import pytest

from payments.client import PaymentFailed, charge_with_retry


class FakeGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def charge(self, txn_id, amount, timeout):
        self.calls.append((txn_id, amount, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_success_on_first_attempt_does_not_sleep():
    gateway, sleeps = FakeGateway(["ch_1"]), []
    assert charge_with_retry(gateway, "t1", 9.5, sleep=sleeps.append) == "ch_1"
    assert gateway.calls == [("t1", 9.5, 5.0)]
    assert sleeps == []


def test_timeouts_are_retried_with_exponential_backoff():
    gateway, sleeps = FakeGateway([TimeoutError(), TimeoutError(), "ch_3"]), []
    assert charge_with_retry(gateway, "t1", 9.5, sleep=sleeps.append) == "ch_3"
    assert len(gateway.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_gives_up_after_all_attempts_without_a_final_sleep():
    gateway, sleeps = FakeGateway([TimeoutError()] * 3), []
    with pytest.raises(PaymentFailed, match="gave up after 3 attempts") as info:
        charge_with_retry(gateway, "t1", 9.5, sleep=sleeps.append)
    assert isinstance(info.value.__cause__, TimeoutError)
    assert len(gateway.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_single_attempt_means_no_retry():
    gateway, sleeps = FakeGateway([TimeoutError()]), []
    with pytest.raises(PaymentFailed):
        charge_with_retry(gateway, "t1", 9.5, attempts=1, sleep=sleeps.append)
    assert len(gateway.calls) == 1
    assert sleeps == []


def test_declines_are_not_retried():
    gateway = FakeGateway([PermissionError("card declined"), "never"])
    with pytest.raises(PermissionError):
        charge_with_retry(gateway, "t1", 9.5, sleep=lambda s: None)
    assert len(gateway.calls) == 1


def test_attempts_must_be_positive():
    with pytest.raises(ValueError):
        charge_with_retry(FakeGateway([]), "t1", 9.5, attempts=0)
