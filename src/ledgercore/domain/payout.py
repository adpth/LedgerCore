"""External-payout saga: a pure state machine (no DB, no IO).

Happy path, each arrow one durable step (ledger entry + status change in one transaction)::

    PENDING --hold entry--> HELD --record "calling provider"--> PROVIDER_PENDING
        --provider SUCCEEDED, payout entry--> COMPLETED

Other paths::

    PENDING            -> REJECTED               hold could not be posted (e.g. insufficient
                                                 funds). Nothing was held; nothing to undo.
    PROVIDER_PENDING   -> FAILED                 provider said FAILED (definitive).
    FAILED             -> RELEASED               hold_release entry posted; funds returned.
    PROVIDER_PENDING   -> NEEDS_RECONCILIATION   provider outcome UNKNOWN (timeout, error,
                                                 ambiguous reply).
    NEEDS_RECONCILIATION -> COMPLETED | FAILED   a later idempotent submit/get_status gave a
                                                 definitive answer.

Safety rule: **an UNKNOWN provider outcome must never release the hold.** After a timeout
the provider may already have sent the money; releasing would hand the customer the funds
a second time and leave the ledger short of real money. So UNKNOWN parks the payout in
NEEDS_RECONCILIATION with the funds still held, and the only edge into RELEASED is from
FAILED, which is entered only on a definitive provider FAILURE. Holding too long is a
recoverable inconvenience; releasing money that left is a loss.

Why FAILED is separate from RELEASED: recording the provider's verdict and posting the
compensating entry are separate steps, so a crash in between leaves FAILED (release still
owed, retry it) rather than an ambiguous state. REJECTED is separate from FAILED because
there is no hold to release.

Terminal states: COMPLETED, RELEASED, REJECTED. NEEDS_RECONCILIATION is *not* terminal.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from ledgercore.domain.entry import _validate_account_id
from ledgercore.domain.errors import (
    IllegalPayoutTransition,
    InvalidAmount,
    InvalidEntry,
    InvalidPayout,
)
from ledgercore.domain.money import Money
from ledgercore.domain.provider import ProviderOutcome, ProviderResult


class PayoutStatus(enum.StrEnum):
    PENDING = "PENDING"
    HELD = "HELD"
    PROVIDER_PENDING = "PROVIDER_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


S = PayoutStatus
TRANSITIONS: dict[PayoutStatus, frozenset[PayoutStatus]] = {
    S.PENDING: frozenset({S.HELD, S.REJECTED}),
    S.HELD: frozenset({S.PROVIDER_PENDING}),
    S.PROVIDER_PENDING: frozenset({S.COMPLETED, S.FAILED, S.NEEDS_RECONCILIATION}),
    S.NEEDS_RECONCILIATION: frozenset({S.COMPLETED, S.FAILED}),
    S.FAILED: frozenset({S.RELEASED}),
    S.COMPLETED: frozenset(),
    S.RELEASED: frozenset(),
    S.REJECTED: frozenset(),
}
TERMINAL: frozenset[PayoutStatus] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

# Status a payout moves to once the provider reports each outcome.
_ON_PROVIDER_OUTCOME = {
    ProviderOutcome.SUCCEEDED: S.COMPLETED,
    ProviderOutcome.FAILED: S.FAILED,
    ProviderOutcome.UNKNOWN: S.NEEDS_RECONCILIATION,
}


def can_transition(src: PayoutStatus, dst: PayoutStatus) -> bool:
    return dst in TRANSITIONS[src]


def transition(src: PayoutStatus, dst: PayoutStatus) -> PayoutStatus:
    """Return ``dst`` if ``src -> dst`` is allowed, else raise ``IllegalPayoutTransition``."""
    if not can_transition(src, dst):
        raise IllegalPayoutTransition(f"payout cannot go from {src} to {dst}")
    return dst


@dataclass(frozen=True, slots=True)
class Payout:
    id: str
    account_id: int
    amount: Money
    destination: str
    status: PayoutStatus = PayoutStatus.PENDING
    provider_reference: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise InvalidPayout("payout id must be a non-empty str")
        if not isinstance(self.destination, str) or not self.destination:
            raise InvalidPayout("destination must be a non-empty str")
        try:
            _validate_account_id(self.account_id)
        except InvalidEntry as exc:
            raise InvalidPayout(str(exc)) from exc
        if not isinstance(self.amount, Money) or not self.amount.is_positive:
            raise InvalidAmount(f"payout amount must be positive Money, got {self.amount!r}")
        object.__setattr__(self, "status", PayoutStatus(self.status))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def advance(
        self,
        to: PayoutStatus,
        *,
        provider_reference: str | None = None,
        failure_reason: str | None = None,
    ) -> Payout:
        """Return a copy in status ``to``; fields given here overwrite, others are kept."""
        return replace(
            self,
            status=transition(self.status, to),
            provider_reference=provider_reference or self.provider_reference,
            failure_reason=failure_reason or self.failure_reason,
        )

    def on_provider_result(self, result: ProviderResult) -> Payout:
        """Apply a provider answer (from PROVIDER_PENDING or NEEDS_RECONCILIATION).

        UNKNOWN while already in NEEDS_RECONCILIATION is a no-op: still unknown, still held.
        """
        to = _ON_PROVIDER_OUTCOME[result.outcome]
        if to is S.NEEDS_RECONCILIATION and self.status is S.NEEDS_RECONCILIATION:
            return self
        reason = result.reason if result.outcome is ProviderOutcome.FAILED else None
        return self.advance(to, provider_reference=result.reference, failure_reason=reason)
