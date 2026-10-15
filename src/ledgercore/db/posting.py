"""Core posting routine plus balance-projection rebuild / drift detection.

Conventions: ``amount_minor`` is signed integer minor units, debit positive and credit
negative; an account's balance is the SUM of its line amounts. A transfer A -> B of X is
A: -X, B: +X.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledgercore.db.errors import (
    BalanceOverflow,
    CurrencyMismatch,
    EntryAlreadyReversed,
    InsufficientFunds,
    UnbalancedEntry,
    UnknownAccount,
    UnknownEntry,
)
from ledgercore.db.models import Account, Balance, EntryLine, JournalEntry
from ledgercore.outbox.writer import write_outbox_event

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_BIGINT_MIN, _BIGINT_MAX = -(2**63), 2**63 - 1


@dataclass(frozen=True, slots=True)
class LineInput:
    account_id: int
    amount_minor: int  # signed: debit positive, credit negative
    currency: str


@dataclass(frozen=True, slots=True)
class PostedEntry:
    entry_id: int
    balances: dict[int, int]  # account_id -> new balance_minor


@dataclass(frozen=True, slots=True)
class Drift:
    account_id: int
    projected_minor: int
    computed_minor: int


def _validate_shape(lines: Sequence[LineInput]) -> None:
    if len(lines) < 2:
        raise UnbalancedEntry("an entry needs at least 2 lines")
    sums: dict[str, int] = defaultdict(int)
    for line in lines:
        if isinstance(line.amount_minor, bool) or not isinstance(line.amount_minor, int):
            raise UnbalancedEntry("amount_minor must be an int (minor units)")
        if line.amount_minor == 0:
            raise UnbalancedEntry("line amount must be nonzero")
        if not _CURRENCY_RE.match(line.currency):
            raise UnbalancedEntry(f"invalid currency code {line.currency!r}")
        sums[line.currency] += line.amount_minor
    bad = {c: s for c, s in sums.items() if s != 0}
    if bad:
        raise UnbalancedEntry(f"lines do not sum to zero per currency: {bad}")


async def post_entry(
    session: AsyncSession,
    entry_type: str,
    lines: Sequence[LineInput],
    reverses_entry_id: int | None = None,
    description: str | None = None,
) -> PostedEntry:
    """Post a balanced journal entry inside the caller's transaction (never commits).

    Lock ordering: every affected account row is locked with ``SELECT ... FOR UPDATE``
    in ASCENDING account id order. A deadlock needs a cycle in the wait-for graph, i.e.
    T1 holding A while wanting B and T2 holding B while wanting A. If all transactions
    acquire locks in the same global order, a transaction holding the lock on B can only
    be waiting for accounts with an id greater than B, so no cycle can form. Hence
    concurrent A->B and B->A transfers serialize instead of deadlocking. (The ORDER BY
    sits below the LockRows node in Postgres' plan, so rows are locked in sort order.)

    The lock is taken before the balance is read, so the non-negative check runs against
    a balance nobody else can change until we commit: no lost updates, no overdraw.
    """
    _validate_shape(lines)

    deltas: dict[int, int] = defaultdict(int)
    for line in lines:
        deltas[line.account_id] += line.amount_minor
    account_ids = sorted({line.account_id for line in lines})

    acct_rows = (
        await session.execute(
            select(Account.id, Account.currency, Account.allow_negative)
            .where(Account.id.in_(account_ids))
            .order_by(Account.id)
            .with_for_update()
        )
    ).all()
    accounts = {r.id: r for r in acct_rows}
    missing = [i for i in account_ids if i not in accounts]
    if missing:
        raise UnknownAccount(missing)
    for line in lines:
        acct = accounts[line.account_id]
        if acct.currency != line.currency:
            raise CurrencyMismatch(line.account_id, acct.currency, line.currency)

    if reverses_entry_id is not None:
        exists = await session.scalar(
            select(JournalEntry.id).where(JournalEntry.id == reverses_entry_id)
        )
        if exists is None:
            raise UnknownEntry(reverses_entry_id)
        already = await session.scalar(
            select(JournalEntry.id).where(JournalEntry.reverses_entry_id == reverses_entry_id)
        )
        if already is not None:
            raise EntryAlreadyReversed(reverses_entry_id)

    bal_rows = (
        await session.execute(
            select(Balance.account_id, Balance.balance_minor)
            .where(Balance.account_id.in_(account_ids))
            .order_by(Balance.account_id)
            .with_for_update()
        )
    ).all()
    current = {r.account_id: r.balance_minor for r in bal_rows}
    new_balances: dict[int, int] = {}
    for account_id in account_ids:
        old = current.get(account_id, 0)
        new = old + deltas[account_id]
        if new < 0 and not accounts[account_id].allow_negative:
            raise InsufficientFunds(account_id, old, deltas[account_id])
        if not _BIGINT_MIN <= new <= _BIGINT_MAX:
            raise BalanceOverflow(account_id, old, deltas[account_id])
        new_balances[account_id] = new

    try:
        # Savepoint so a unique-violation race on reverses_entry_id (backstop to the
        # check above) doesn't poison the caller's whole transaction.
        async with session.begin_nested():
            inserted = (
                await session.execute(
                    insert(JournalEntry)
                    .values(
                        entry_type=entry_type,
                        reverses_entry_id=reverses_entry_id,
                        description=description,
                    )
                    .returning(JournalEntry.id, JournalEntry.created_at)
                )
            ).one()
    except IntegrityError as exc:
        if reverses_entry_id is not None and "reverses_entry_id" in str(exc.orig):
            raise EntryAlreadyReversed(reverses_entry_id) from exc
        raise
    entry_id: int = inserted.id

    await session.execute(
        insert(EntryLine),
        [
            {
                "entry_id": entry_id,
                "account_id": line.account_id,
                "amount_minor": line.amount_minor,
                "currency": line.currency,
            }
            for line in lines
        ],
    )
    for account_id in account_ids:
        await session.execute(
            update(Balance)
            .where(Balance.account_id == account_id)
            .values(
                balance_minor=new_balances[account_id],
                version=Balance.version + 1,
                updated_at=func.now(),
            )
        )
    # Same transaction as the entry: the event exists if and only if the entry commits.
    await write_outbox_event(
        session,
        aggregate_type="journal_entry",
        aggregate_id=str(entry_id),
        event_type="entry.posted",
        payload={
            "entry_id": entry_id,
            "entry_type": entry_type,
            "reverses_entry_id": reverses_entry_id,
            "lines": [
                {
                    "account_id": line.account_id,
                    "amount_minor": line.amount_minor,
                    "currency": line.currency,
                }
                for line in lines
            ],
            "created_at": inserted.created_at.isoformat(),
        },
    )
    return PostedEntry(entry_id=entry_id, balances=new_balances)


_DRIFT_SQL = text(
    """
    SELECT a.id AS account_id,
           COALESCE(b.balance_minor, 0)::numeric AS projected,
           COALESCE(l.total, 0)::numeric AS computed
    FROM accounts a
    LEFT JOIN balances b ON b.account_id = a.id
    LEFT JOIN (SELECT account_id, SUM(amount_minor) AS total
               FROM entry_lines GROUP BY account_id) l ON l.account_id = a.id
    WHERE COALESCE(b.balance_minor, 0) IS DISTINCT FROM COALESCE(l.total, 0)
    ORDER BY a.id
    """
)


async def find_drift(session: AsyncSession) -> list[Drift]:
    """Accounts whose projected balance differs from SUM(entry_lines)."""
    rows = (await session.execute(_DRIFT_SQL)).all()
    return [Drift(r.account_id, int(r.projected), int(r.computed)) for r in rows]


async def rebuild_balances(session: AsyncSession) -> list[Drift]:
    """Recompute the projection from lines; returns the drifts that were repaired.

    Locks candidate accounts in ascending id order (same rule as post_entry), then
    re-reads the drift under the locks so a posting that committed meanwhile is not
    overwritten with a stale value. Caller commits.
    """
    candidates = await find_drift(session)
    if not candidates:
        return []
    ids = [d.account_id for d in candidates]
    await session.execute(
        select(Account.id).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()
    )
    locked = set(ids)
    drifts = [d for d in await find_drift(session) if d.account_id in locked]
    for d in drifts:
        await session.execute(
            update(Balance)
            .where(Balance.account_id == d.account_id)
            .values(
                balance_minor=d.computed_minor,
                version=Balance.version + 1,
                updated_at=func.now(),
            )
        )
    return drifts
