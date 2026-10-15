"""External-payout saga as a sequence of short DB transactions (docs/adr/0008-payout-saga.md).

The pure state machine lives in ``domain.payout``; this module drives it against Postgres.
Rules every step follows:

* One step = ONE transaction that locks the payout row (``SELECT ... FOR UPDATE``), re-checks
  the status it expects, then writes its ledger entry (if any), the status change and its
  outbox event together. Re-running a step whose work is already done is a no-op, so every
  step is idempotent and the saga is resumable from whatever status is committed.
* Lock order is always payout row first, then accounts (``post_entry`` locks them in
  ascending id order, ADR 0003), so the two lock families cannot deadlock.
* ``provider.submit`` / ``get_status`` run OUTSIDE any transaction and never while a row lock
  is held. PROVIDER_PENDING is committed *before* the call, so a crash after the call
  leaves a durable marker that the recovery worker finds.
* An UNKNOWN provider answer (or an exception) NEVER releases the hold: it parks the payout
  in NEEDS_RECONCILIATION with the funds still held (safety rule in ``domain.payout``).

Events (aggregate ``payout``): payout.held, payout.completed, payout.failed, payout.released,
payout.needs_reconciliation. PROVIDER_PENDING and REJECTED emit none.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgercore.db.errors import CurrencyMismatch, InsufficientFunds, UnknownAccount
from ledgercore.db.models import Account, JournalEntry, PayoutRow
from ledgercore.db.posting import LineInput, post_entry
from ledgercore.domain import (
    Entry,
    Money,
    Payout,
    PayoutProvider,
    PayoutStatus,
    ProviderOutcome,
    ProviderResult,
    transition,
)
from ledgercore.outbox.writer import write_outbox_event
from ledgercore.services.errors import PayoutNotFound, PayoutNotResolvable
from ledgercore.services.ledger import _require_customer_accounts, get_or_create_system_account

log = logging.getLogger(__name__)
S = PayoutStatus
SM = async_sessionmaker[AsyncSession]

INSUFFICIENT_FUNDS = "insufficient_funds"
_MAX_STEPS = 12  # a payout has at most ~6 durable steps; this only stops a logic bug looping


@dataclass(frozen=True, slots=True)
class PayoutView:
    id: str
    account_id: int
    amount_minor: int
    currency: str
    destination: str
    status: PayoutStatus
    hold_entry_id: int | None
    final_entry_id: int | None
    release_entry_id: int | None
    provider_reference: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    version: int

    @property
    def money(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def is_terminal(self) -> bool:
        return self.status in (S.COMPLETED, S.RELEASED, S.REJECTED)


def _view(row: Any) -> PayoutView:
    data = dict(row._mapping)
    data["status"] = PayoutStatus(data["status"])
    return PayoutView(**data)


async def get_payout(session: AsyncSession, payout_id: str, *, lock: bool = False) -> PayoutView:
    stmt = select(PayoutRow.__table__).where(PayoutRow.id == payout_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise PayoutNotFound(payout_id)
    return _view(row)


async def _lock_skip(session: AsyncSession, payout_id: str) -> PayoutView | None:
    """Lock the row, or return None if another transaction holds it (recovery workers)."""
    row = (
        await session.execute(
            select(PayoutRow.__table__)
            .where(PayoutRow.id == payout_id)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    return None if row is None else _view(row)


async def _move(
    session: AsyncSession,
    p: PayoutView,
    to: PayoutStatus,
    event_type: str | None = None,
    **fields: Any,
) -> None:
    """Status change + event, under the row lock the caller holds. Legal edges only."""
    transition(p.status, to)  # domain rule; the DB trigger re-checks it
    await session.execute(
        update(PayoutRow)
        .where(PayoutRow.id == p.id, PayoutRow.status == p.status.value)
        .values(status=to.value, version=PayoutRow.version + 1, updated_at=func.now(), **fields)
    )
    if event_type is not None:
        payload: dict[str, Any] = {
            "payout_id": p.id,
            "account_id": p.account_id,
            "amount_minor": p.amount_minor,
            "currency": p.currency,
            "destination": p.destination,
            "status": to.value,
            "from_status": p.status.value,
        }
        payload.update({k: v for k, v in fields.items() if v is not None})
        await write_outbox_event(session, "payout", p.id, event_type, payload)


async def _hold(session: AsyncSession, p: PayoutView, holds: int | None = None) -> None:
    """Post the HOLD for a PENDING payout (or REJECT it). Caller holds the row lock."""
    if holds is None:
        holds = await get_or_create_system_account(session, p.currency, "holds")
    hold_entry = Entry.hold(p.account_id, holds, p.money, description=f"payout:{p.id}:hold")
    try:
        # Savepoint: InsufficientFunds is raised before any write, but stay safe anyway.
        async with session.begin_nested():
            posted = await post_entry(
                session,
                hold_entry.entry_type.value,
                [LineInput(ln.account_id, ln.amount_minor, ln.currency) for ln in hold_entry.lines],
                description=hold_entry.description,
            )
    except InsufficientFunds:
        await _move(session, p, S.REJECTED, failure_reason=INSUFFICIENT_FUNDS)
        return
    await _move(session, p, S.HELD, "payout.held", hold_entry_id=posted.entry_id)


async def create_payout(
    session: AsyncSession, account_id: int, amount: Money, destination: str
) -> PayoutView:
    """Step 1, in the caller's transaction: create the payout and post its HOLD.

    Ends HELD, or REJECTED when the customer cannot cover it (nothing held). Unknown or
    system accounts and currency mismatches raise and roll everything back.
    """
    payout_id = str(uuid.uuid4())
    Payout(payout_id, account_id, amount, destination)  # domain validation only
    await _require_customer_accounts(session, [account_id])
    currency = await session.scalar(select(Account.currency).where(Account.id == account_id))
    if currency is None:  # pragma: no cover - checked just above
        raise UnknownAccount([account_id])
    if currency != amount.currency:
        raise CurrencyMismatch(account_id, currency, amount.currency)
    # Lock order matters here. Inserting the payout row takes a KEY SHARE lock on the customer
    # account (FK), which conflicts with the FOR UPDATE that post_entry wants next: two
    # payouts for one account would each hold KEY SHARE and wait on the other (deadlock).
    # So: find/create the holds account first (it may wait on another creator; we hold
    # nothing relevant yet), lock customer + holds in ascending id order exactly like
    # post_entry (ADR 0003), and only then insert the payout.
    holds = await get_or_create_system_account(session, amount.currency, "holds")
    await session.execute(
        select(Account.id)
        .where(Account.id.in_([account_id, holds]))
        .order_by(Account.id)
        .with_for_update()
    )
    await session.execute(
        insert(PayoutRow).values(
            id=payout_id,
            account_id=account_id,
            amount_minor=amount.amount_minor,
            currency=amount.currency,
            destination=destination,
            status=S.PENDING.value,
        )
    )
    await _hold(session, await get_payout(session, payout_id, lock=True), holds)
    return await get_payout(session, payout_id)


class PayoutSaga:
    """Drives payouts through the saga. ``fault`` is a test hook called at step boundaries
    (``fault("after_provider_call")`` etc.); raising from it simulates a crash there."""

    def __init__(
        self,
        sm: SM,
        provider: PayoutProvider,
        fault: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._sm = sm
        self._provider = provider
        self._fault = fault

    async def _checkpoint(self, name: str) -> None:
        if self._fault is not None:
            out = self._fault(name)
            if out is not None:
                await out

    async def get(self, payout_id: str) -> PayoutView:
        async with self._sm() as s, s.begin():
            return await get_payout(s, payout_id)

    # ---- steps (each one transaction, each idempotent) ----------------------------

    async def _lock(self, s: AsyncSession, payout_id: str, skip: bool) -> PayoutView | None:
        return await (_lock_skip(s, payout_id) if skip else get_payout(s, payout_id, lock=True))

    async def step_hold(self, payout_id: str, *, skip_locked: bool = False) -> None:
        """PENDING -> HELD | REJECTED. Only reachable for a PENDING row that was committed
        by an older/other writer; ``create_payout`` normally finishes this atomically.
        A hold posted earlier (found by its description) is linked instead of posted twice."""
        async with self._sm() as s, s.begin():
            p = await self._lock(s, payout_id, skip_locked)
            if p is None or p.status is not S.PENDING:
                return
            existing = await s.scalar(
                select(JournalEntry.id).where(
                    JournalEntry.entry_type == "HOLD",
                    JournalEntry.description == f"payout:{p.id}:hold",
                )
            )
            if existing is not None:
                await _move(s, p, S.HELD, "payout.held", hold_entry_id=existing)
            else:
                await _hold(s, p)

    async def step_mark_provider_pending(
        self, payout_id: str, *, skip_locked: bool = False
    ) -> PayoutView | None:
        """HELD -> PROVIDER_PENDING, committed before the provider is called."""
        async with self._sm() as s, s.begin():
            p = await self._lock(s, payout_id, skip_locked)
            if p is None or p.status is not S.HELD:
                return None
            await _move(s, p, S.PROVIDER_PENDING)
            return await get_payout(s, payout_id)

    async def _ask_provider(self, p: PayoutView, *, status_first: bool) -> ProviderResult:
        """No DB session is open here. Exceptions become UNKNOWN (never a release)."""
        try:
            if status_first:
                known = await self._provider.get_status(p.id)
                if known.outcome is not ProviderOutcome.UNKNOWN:
                    return known
            return await self._provider.submit(p.id, p.money, p.destination)
        except Exception as exc:
            log.warning("provider call for payout %s failed: %r", p.id, exc)
            return ProviderResult(ProviderOutcome.UNKNOWN, reason=f"{type(exc).__name__}: {exc}")

    async def step_apply_result(
        self, payout_id: str, result: ProviderResult, *, resolved_by_operator: bool = False
    ) -> PayoutView:
        """PROVIDER_PENDING | NEEDS_RECONCILIATION + provider answer -> COMPLETED, FAILED or
        NEEDS_RECONCILIATION, with the PAYOUT entry for COMPLETED, in one transaction."""
        async with self._sm() as s, s.begin():
            p = await get_payout(s, payout_id, lock=True)
            if p.status not in (S.PROVIDER_PENDING, S.NEEDS_RECONCILIATION):
                # Already applied by someone else (or never reached). Not an error, but a
                # contradictory answer for a settled payout is worth shouting about.
                paid = p.status is S.COMPLETED
                if result.outcome is not ProviderOutcome.UNKNOWN and (
                    paid != (result.outcome is ProviderOutcome.SUCCEEDED)
                ):
                    log.error(
                        "payout %s is %s but provider now says %s; not changing money",
                        p.id,
                        p.status,
                        result.outcome,
                    )
                return p
            domain = Payout(
                p.id, p.account_id, p.money, p.destination, p.status
            ).on_provider_result(result)
            if domain.status is p.status:  # UNKNOWN again while NEEDS_RECONCILIATION
                return p
            details: dict[str, Any] = {
                "provider_reference": domain.provider_reference,
                "failure_reason": domain.failure_reason or result.reason,
            }
            if domain.status is S.COMPLETED:
                holds = await get_or_create_system_account(s, p.currency, "holds")
                clearing = await get_or_create_system_account(s, p.currency, "clearing")
                entry = Entry.payout(holds, clearing, p.money, description=f"payout:{p.id}")
                posted = await post_entry(
                    s,
                    entry.entry_type.value,
                    [LineInput(ln.account_id, ln.amount_minor, ln.currency) for ln in entry.lines],
                    description=entry.description,
                )
                await self._checkpoint("in_apply_tx")  # crash before commit: all rolls back
                await _move(
                    s,
                    p,
                    S.COMPLETED,
                    "payout.completed",
                    final_entry_id=posted.entry_id,
                    **_non_null({"provider_reference": details["provider_reference"]}),
                )
            elif domain.status is S.FAILED:
                await _move(s, p, S.FAILED, "payout.failed", **_non_null(details))
            else:
                await _move(
                    s,
                    p,
                    S.NEEDS_RECONCILIATION,
                    "payout.needs_reconciliation",
                    **_non_null({"failure_reason": result.reason}),
                )
            return await get_payout(s, payout_id)

    async def step_release(self, payout_id: str, *, skip_locked: bool = False) -> None:
        """FAILED -> RELEASED: HOLD_RELEASE (exact negation of the hold) + status, one tx."""
        async with self._sm() as s, s.begin():
            p = await self._lock(s, payout_id, skip_locked)
            if p is None or p.status is not S.FAILED:
                return
            assert p.hold_entry_id is not None
            holds = await get_or_create_system_account(s, p.currency, "holds")
            entry = Entry.hold_release(
                p.account_id, holds, p.money, p.hold_entry_id, description=f"payout:{p.id}:release"
            )
            posted = await post_entry(
                s,
                entry.entry_type.value,
                [LineInput(ln.account_id, ln.amount_minor, ln.currency) for ln in entry.lines],
                reverses_entry_id=entry.reverses_entry_id,
                description=entry.description,
            )
            await _move(s, p, S.RELEASED, "payout.released", release_entry_id=posted.entry_id)

    # ---- driver ---------------------------------------------------------------------

    async def advance(self, payout_id: str, *, recovering: bool = False) -> PayoutView:
        """Push a payout as far as it can go right now; safe to call at any time, any number
        of times, concurrently. Stops at a terminal state, NEEDS_RECONCILIATION (unless
        ``recovering``, which re-asks the provider via ``get_status``) or when another worker
        owns the row. ``recovering`` also makes DB-only steps skip rows locked by others and
        asks the provider's status before re-submitting."""
        p = await self.get(payout_id)
        for _ in range(_MAX_STEPS):
            before = p.status
            if p.status is S.PENDING:
                await self.step_hold(p.id, skip_locked=recovering)
            elif p.status is S.HELD:
                await self._checkpoint("after_hold")
                await self.step_mark_provider_pending(p.id, skip_locked=recovering)
            elif p.status is S.PROVIDER_PENDING:
                await self._checkpoint("after_provider_pending")
                result = await self._ask_provider(p, status_first=recovering)
                await self._checkpoint("after_provider_call")
                await self.step_apply_result(p.id, result)
            elif p.status is S.NEEDS_RECONCILIATION and recovering:
                try:
                    result = await self._provider.get_status(p.id)
                except Exception as exc:
                    log.warning("get_status for payout %s failed: %r", p.id, exc)
                    result = ProviderResult(ProviderOutcome.UNKNOWN, reason=repr(exc))
                await self.step_apply_result(p.id, result)
            elif p.status is S.FAILED:
                await self._checkpoint("after_failed")
                await self.step_release(p.id, skip_locked=recovering)
            p = await self.get(p.id)
            if p.status is before:
                break  # terminal, parked, or busy elsewhere
        return p

    async def resolve(self, payout_id: str, outcome: str) -> PayoutView:
        """Operator verdict for a NEEDS_RECONCILIATION payout: ``succeeded`` -> COMPLETED,
        ``failed`` -> FAILED then RELEASED. Repeating the same verdict is a no-op; a
        different verdict, or a payout not awaiting one, raises PayoutNotResolvable.
        Unauthenticated until Phase 3; the operator is trusted to have checked the provider."""
        succeeded = outcome == "succeeded"
        p = await self.get(payout_id)
        settled = {S.COMPLETED} if succeeded else {S.FAILED, S.RELEASED}
        if p.status in settled:
            return await self.advance(payout_id)  # finishes a FAILED release if one is owed
        if p.status is not S.NEEDS_RECONCILIATION:
            raise PayoutNotResolvable(payout_id, p.status.value, outcome)
        result = (
            ProviderResult(ProviderOutcome.SUCCEEDED, reference=p.provider_reference)
            if succeeded
            else ProviderResult(ProviderOutcome.FAILED, reason="resolved failed by operator")
        )
        after = await self.step_apply_result(payout_id, result)
        if after.status not in settled:  # lost a race to the opposite verdict
            raise PayoutNotResolvable(payout_id, after.status.value, outcome)
        return await self.advance(payout_id)


def _non_null(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}
