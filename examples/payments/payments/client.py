"""Charging a card through a flaky payment gateway, with retries on timeouts."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol


class Gateway(Protocol):
    def charge(self, txn_id: str, amount: float, timeout: float) -> str: ...


class PaymentFailed(RuntimeError):
    """The charge did not go through after all attempts."""


def charge_with_retry(
    gateway: Gateway,
    txn_id: str,
    amount: float,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Charge ``amount``; retry timeouts with exponential backoff (0.5s, 1s, ...); declines are not retried."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return gateway.charge(txn_id, amount, timeout=5.0)
        except TimeoutError as exc:
            last_error = exc
            if attempt < attempts - 1:
                sleep(0.5 * 2**attempt)
    raise PaymentFailed(f"{txn_id}: gave up after {attempts} attempts") from last_error
