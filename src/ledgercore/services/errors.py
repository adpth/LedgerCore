"""Errors raised by the service layer (on top of ``domain.errors`` and ``db.errors``)."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer errors."""


class IdempotencyKeyReuse(ServiceError):  # noqa: N818 - domain vocabulary
    """The key was already used with a different request (method, path or body)."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"idempotency key {key!r} was already used with a different request")


class IdempotencyKeyInProgress(ServiceError):  # noqa: N818
    """A committed PENDING row was observed. Unreachable with ``run_idempotent`` (which
    commits the claim and the completion together) but handled defensively."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"a request with idempotency key {key!r} is still in progress")


class SystemAccountNotAllowed(ServiceError):  # noqa: N818
    """Client tried to move money directly in/out of an internal system account."""

    def __init__(self, account_id: int) -> None:
        self.account_id = account_id
        super().__init__(f"account {account_id} is a system account")


class EntryNotReversible(ServiceError):  # noqa: N818
    """The entry belongs to the payout saga and may only be undone by the saga itself."""

    def __init__(self, entry_id: int, entry_type: str) -> None:
        self.entry_id = entry_id
        self.entry_type = entry_type
        super().__init__(f"{entry_type} entry {entry_id} cannot be reversed directly")


class PayoutNotFound(ServiceError):  # noqa: N818
    def __init__(self, payout_id: str) -> None:
        self.payout_id = payout_id
        super().__init__(f"unknown payout: {payout_id}")


class PayoutNotResolvable(ServiceError):  # noqa: N818
    """Operator resolution asked for a payout that is not (or no longer) awaiting it."""

    def __init__(self, payout_id: str, status: str, outcome: str) -> None:
        self.payout_id = payout_id
        self.status = status
        super().__init__(f"payout {payout_id} is {status}; cannot resolve as {outcome}")
