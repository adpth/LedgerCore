"""Pure double-entry ledger domain (no DB, no IO)."""

from ledgercore.domain.entry import Entry, EntryType, Line
from ledgercore.domain.errors import (
    CurrencyMismatch,
    DomainError,
    IllegalPayoutTransition,
    InvalidAmount,
    InvalidCurrency,
    InvalidEntry,
    InvalidPayout,
    SameAccountTransfer,
    UnbalancedEntry,
)
from ledgercore.domain.idempotency import request_fingerprint
from ledgercore.domain.money import Money
from ledgercore.domain.payout import TERMINAL, Payout, PayoutStatus, can_transition, transition
from ledgercore.domain.provider import (
    FakeBehavior,
    FakePayoutProvider,
    PayoutProvider,
    ProviderOutcome,
    ProviderResult,
)

__all__ = [
    "TERMINAL",
    "CurrencyMismatch",
    "DomainError",
    "Entry",
    "EntryType",
    "FakeBehavior",
    "FakePayoutProvider",
    "IllegalPayoutTransition",
    "InvalidAmount",
    "InvalidCurrency",
    "InvalidEntry",
    "InvalidPayout",
    "Line",
    "Money",
    "Payout",
    "PayoutProvider",
    "PayoutStatus",
    "ProviderOutcome",
    "ProviderResult",
    "SameAccountTransfer",
    "UnbalancedEntry",
    "can_transition",
    "request_fingerprint",
    "transition",
]
