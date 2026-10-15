"""External payout provider port, plus a scriptable in-memory fake.

Contract every ``PayoutProvider`` implementation must honour:

* **Idempotency by ``payout_id``.** Submitting the same ``payout_id`` any number of times
  pays at most once. A repeat submit of an already-paid id reports ``SUCCEEDED`` with the
  original reference; of a definitively failed id, ``FAILED`` again. This is what makes it
  safe for the saga to retry ``submit`` after a timeout or crash.
* **``FAILED`` is definitive**: the provider guarantees no money left and none ever will
  for this ``payout_id``. Only this outcome lets the saga release the hold.
* **``UNKNOWN`` promises nothing**: the money may or may not have left (still processing,
  timed out, ambiguous reply). A raised exception from ``submit`` must be treated the same.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol

from ledgercore.domain.money import Money


class ProviderOutcome(enum.StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    outcome: ProviderOutcome
    reference: str | None = None
    reason: str | None = None


class PayoutProvider(Protocol):
    async def submit(self, payout_id: str, amount: Money, destination: str) -> ProviderResult:
        """Pay ``amount`` to ``destination``; idempotent by ``payout_id`` (see module doc)."""
        ...

    async def get_status(self, payout_id: str) -> ProviderResult:
        """Report the provider's current view of ``payout_id`` (UNKNOWN if never seen)."""
        ...


class FakeBehavior(enum.StrEnum):
    SUCCEED = "SUCCEED"
    FAIL = "FAIL"
    TIMEOUT_AFTER_SUCCESS = "TIMEOUT_AFTER_SUCCESS"  # pays, but the caller sees UNKNOWN
    TIMEOUT_WITHOUT_PAYING = "TIMEOUT_WITHOUT_PAYING"  # nothing paid, caller sees UNKNOWN
    RAISE = "RAISE"  # nothing paid, submit raises ConnectionError


@dataclass(frozen=True, slots=True)
class FakePayment:
    payout_id: str
    amount: Money
    destination: str
    reference: str


@dataclass
class FakePayoutProvider:
    """In-memory ``PayoutProvider``. Unscripted ids SUCCEED.

    ``script(payout_id, behavior)`` sets what the *next* first-time submit of that id does;
    once an id is paid or definitively failed, every later submit just reports that.
    """

    behaviors: dict[str, FakeBehavior] = field(default_factory=dict)
    payments: dict[str, FakePayment] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    submit_calls: list[tuple[str, Money, str]] = field(default_factory=list)
    status_calls: list[str] = field(default_factory=list)
    money_sent: list[FakePayment] = field(default_factory=list)  # one item per real payment

    def script(self, payout_id: str, behavior: FakeBehavior) -> None:
        self.behaviors[payout_id] = behavior

    def _known(self, payout_id: str) -> ProviderResult | None:
        if payout_id in self.payments:
            return ProviderResult(ProviderOutcome.SUCCEEDED, self.payments[payout_id].reference)
        if payout_id in self.failed:
            return ProviderResult(ProviderOutcome.FAILED, reason=self.failed[payout_id])
        return None

    def _pay(self, payout_id: str, amount: Money, destination: str) -> FakePayment:
        payment = FakePayment(payout_id, amount, destination, f"fake-{payout_id}")
        self.payments[payout_id] = payment
        self.money_sent.append(payment)
        return payment

    async def submit(self, payout_id: str, amount: Money, destination: str) -> ProviderResult:
        self.submit_calls.append((payout_id, amount, destination))
        known = self._known(payout_id)
        if known is not None:
            return known
        behavior = self.behaviors.get(payout_id, FakeBehavior.SUCCEED)
        if behavior is FakeBehavior.SUCCEED:
            payment = self._pay(payout_id, amount, destination)
            return ProviderResult(ProviderOutcome.SUCCEEDED, payment.reference)
        if behavior is FakeBehavior.FAIL:
            self.failed[payout_id] = "rejected by fake provider"
            return ProviderResult(ProviderOutcome.FAILED, reason=self.failed[payout_id])
        if behavior is FakeBehavior.TIMEOUT_AFTER_SUCCESS:
            self._pay(payout_id, amount, destination)
            return ProviderResult(ProviderOutcome.UNKNOWN, reason="timeout")
        if behavior is FakeBehavior.TIMEOUT_WITHOUT_PAYING:
            return ProviderResult(ProviderOutcome.UNKNOWN, reason="timeout")
        raise ConnectionError(f"fake provider unavailable for {payout_id}")

    async def get_status(self, payout_id: str) -> ProviderResult:
        self.status_calls.append(payout_id)
        known = self._known(payout_id)
        return known if known is not None else ProviderResult(ProviderOutcome.UNKNOWN)
