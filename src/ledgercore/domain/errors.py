"""Typed domain errors.

Every error carries a stable, machine-readable ``code`` (class attribute) that the
API layer can map to responses without string-matching messages.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    """Base class for all ledger domain errors."""

    code: ClassVar[str] = "domain_error"


class InvalidAmount(DomainError):
    """Amount is not an int, is a bool/float, or is zero/non-positive where forbidden."""

    code: ClassVar[str] = "invalid_amount"


class InvalidCurrency(DomainError):
    """Currency code is not three upper-case ASCII letters."""

    code: ClassVar[str] = "invalid_currency"


class CurrencyMismatch(DomainError):
    """Operation combined amounts in different currencies."""

    code: ClassVar[str] = "currency_mismatch"


class InvalidEntry(DomainError):
    """Entry is structurally invalid (too few lines, bad account, bad reversal link...)."""

    code: ClassVar[str] = "invalid_entry"


class SameAccountTransfer(InvalidEntry):
    """Transfer source and destination are the same account."""

    code: ClassVar[str] = "same_account_transfer"


class UnbalancedEntry(InvalidEntry):
    """Lines of at least one currency do not sum to zero."""

    code: ClassVar[str] = "unbalanced_entry"


class InvalidPayout(DomainError):
    """Payout fields are invalid (empty id/destination, bad account id...)."""

    code: ClassVar[str] = "invalid_payout"


class IllegalPayoutTransition(DomainError):
    """A payout saga state change that the transition table forbids."""

    code: ClassVar[str] = "illegal_payout_transition"
