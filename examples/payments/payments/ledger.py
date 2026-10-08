"""An idempotent payment ledger: recording the same transaction twice must not double-count it."""

from __future__ import annotations


class DuplicateAmountMismatch(ValueError):
    """The same transaction id was replayed with a different amount."""


class Ledger:
    def __init__(self) -> None:
        self._amounts: dict[str, float] = {}
        self.entries: list[tuple[str, float]] = []

    def record(self, txn_id: str, amount: float) -> bool:
        """Record a payment. Returns False (and changes nothing) for a replay of the same transaction."""
        if not txn_id:
            raise ValueError("txn_id is required")
        if amount <= 0:
            raise ValueError("amount must be positive")
        if txn_id in self._amounts:
            if self._amounts[txn_id] != amount:
                raise DuplicateAmountMismatch(txn_id)
            return False
        self._amounts[txn_id] = amount
        self.entries.append((txn_id, amount))
        return True

    def balance(self) -> float:
        return round(sum(amount for _, amount in self.entries), 2)
