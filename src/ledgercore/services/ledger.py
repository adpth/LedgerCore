"""Ledger use cases built on ``domain.Entry`` constructors and ``db.posting.post_entry``.

All functions run inside the caller's transaction and never commit.

Deposits and withdrawals post against the per-currency *system* (external/settlement)
account, created lazily (see :func:`get_or_create_system_account`). Clients may not name
a system account as the source/target of a deposit, withdrawal or transfer: it allows
negative balances, so moving money out of it would mint money.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledgercore.db.errors import UnknownAccount, UnknownEntry
from ledgercore.db.models import Account, Balance, EntryLine, JournalEntry
from ledgercore.db.posting import LineInput, post_entry
from ledgercore.domain import Entry, Line, Money
from ledgercore.domain.money import validate_currency
from ledgercore.services.errors import EntryNotReversible, SystemAccountNotAllowed

SYSTEM_KINDS = frozenset({"external", "holds", "clearing"})

#: Entry types created by the payout saga; a generic REVERSAL of them would take the unique
#: ``reverses_entry_id`` slot the saga needs (HOLD) or break the saga's accounting.
NON_REVERSIBLE_TYPES = frozenset({"HOLD", "HOLD_RELEASE", "PAYOUT"})


@dataclass(frozen=True, slots=True)
class AccountView:
    id: int
    currency: str
    allow_negative: bool
    is_system: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BalanceView:
    account_id: int
    currency: str
    balance_minor: int


@dataclass(frozen=True, slots=True)
class LineView:
    account_id: int
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class EntryView:
    id: int
    entry_type: str
    description: str | None
    reverses_entry_id: int | None
    created_at: datetime
    lines: tuple[LineView, ...]


# ---- accounts ---------------------------------------------------------------------


async def open_account(session: AsyncSession, currency: str) -> AccountView:
    validate_currency(currency)
    row = (
        await session.execute(
            pg_insert(Account)
            .values(currency=currency, allow_negative=False, is_system=False)
            .returning(
                Account.id,
                Account.currency,
                Account.allow_negative,
                Account.is_system,
                Account.created_at,
            )
        )
    ).one()
    return AccountView(row.id, row.currency, row.allow_negative, row.is_system, row.created_at)


async def get_or_create_system_account(
    session: AsyncSession, currency: str, kind: str = "external"
) -> int:
    """Return the id of the ``kind`` system account for ``currency``, creating it if needed.

    ``kind`` is ``external`` (deposits/withdrawals), ``holds`` or ``clearing`` (payouts).
    Race-safe: the INSERT targets the partial unique index ``(currency, system_kind) WHERE
    is_system``. If a concurrent transaction is creating the same row, Postgres makes us
    wait for it; if it commits we insert nothing and the follow-up SELECT (new READ
    COMMITTED snapshot) sees its row, if it rolls back our insert wins.
    """
    validate_currency(currency)
    if kind not in SYSTEM_KINDS:
        raise ValueError(f"unknown system account kind {kind!r}")
    created = (
        await session.execute(
            pg_insert(Account)
            .values(currency=currency, allow_negative=True, is_system=True, system_kind=kind)
            .on_conflict_do_nothing(
                index_elements=[Account.currency, Account.system_kind],
                index_where=text("is_system"),
            )
            .returning(Account.id)
        )
    ).scalar_one_or_none()
    if created is not None:
        return int(created)
    existing = await session.scalar(
        select(Account.id).where(
            Account.currency == currency, Account.is_system, Account.system_kind == kind
        )
    )
    if existing is None:  # pragma: no cover - would need a concurrent DELETE
        raise RuntimeError(f"system account {kind} for {currency} vanished")
    return int(existing)


async def _require_customer_accounts(session: AsyncSession, account_ids: Sequence[int]) -> None:
    """Raise UnknownAccount / SystemAccountNotAllowed for client-named accounts.

    No lock needed: ``is_system`` never changes and accounts are never deleted;
    ``post_entry`` takes the real row locks afterwards.
    """
    rows = (
        await session.execute(
            select(Account.id, Account.is_system).where(Account.id.in_(account_ids))
        )
    ).all()
    found = {r.id: r.is_system for r in rows}
    missing = sorted(i for i in set(account_ids) if i not in found)
    if missing:
        raise UnknownAccount(missing)
    for account_id in account_ids:
        if found[account_id]:
            raise SystemAccountNotAllowed(account_id)


async def _post(session: AsyncSession, entry: Entry) -> EntryView:
    posted = await post_entry(
        session,
        entry.entry_type.value,
        [LineInput(ln.account_id, ln.amount_minor, ln.currency) for ln in entry.lines],
        reverses_entry_id=entry.reverses_entry_id,
        description=entry.description or None,
    )
    view = await get_entry(session, posted.entry_id)
    assert view is not None
    return view


# ---- money movement ---------------------------------------------------------------


async def deposit(
    session: AsyncSession, account_id: int, amount: Money, description: str = ""
) -> EntryView:
    await _require_customer_accounts(session, [account_id])
    external = await get_or_create_system_account(session, amount.currency)
    return await _post(session, Entry.deposit(external, account_id, amount, description))


async def withdraw(
    session: AsyncSession, account_id: int, amount: Money, description: str = ""
) -> EntryView:
    await _require_customer_accounts(session, [account_id])
    external = await get_or_create_system_account(session, amount.currency)
    return await _post(session, Entry.withdrawal(account_id, external, amount, description))


async def transfer(
    session: AsyncSession,
    from_account_id: int,
    to_account_id: int,
    amount: Money,
    description: str = "",
) -> EntryView:
    # Build first so a same-account transfer fails as SameAccountTransfer, not a lookup.
    entry = Entry.transfer(from_account_id, to_account_id, amount, description)
    await _require_customer_accounts(session, [from_account_id, to_account_id])
    return await _post(session, entry)


async def reverse_entry(
    session: AsyncSession, entry_id: int, description: str | None = None
) -> EntryView:
    """Post a REVERSAL negating every line of ``entry_id``.

    HOLD, HOLD_RELEASE and PAYOUT entries belong to the payout saga and are refused with
    EntryNotReversible. Double reversal is rejected by ``post_entry`` (check under the
    account locks, plus the UNIQUE(reverses_entry_id) backstop) as EntryAlreadyReversed. A
    reversal can fail with InsufficientFunds when the money has since been moved on.
    """
    entry_type = await session.scalar(
        select(JournalEntry.entry_type).where(JournalEntry.id == entry_id)
    )
    if entry_type in NON_REVERSIBLE_TYPES:
        raise EntryNotReversible(entry_id, entry_type)
    rows = (
        await session.execute(
            select(EntryLine.account_id, EntryLine.amount_minor, EntryLine.currency)
            .where(EntryLine.entry_id == entry_id)
            .order_by(EntryLine.id)
        )
    ).all()
    if not rows:
        raise UnknownEntry(entry_id)
    original = [Line(r.account_id, r.amount_minor, r.currency) for r in rows]
    return await _post(session, Entry.reversal_of(original, entry_id, description))


# ---- queries ----------------------------------------------------------------------


async def get_balance(session: AsyncSession, account_id: int) -> BalanceView:
    row = (
        await session.execute(
            select(Balance.account_id, Balance.currency, Balance.balance_minor).where(
                Balance.account_id == account_id
            )
        )
    ).one_or_none()
    if row is None:
        raise UnknownAccount([account_id])
    return BalanceView(row.account_id, row.currency, row.balance_minor)


async def _load_entries(session: AsyncSession, entry_ids: Sequence[int]) -> list[EntryView]:
    if not entry_ids:
        return []
    headers = (
        await session.execute(
            select(
                JournalEntry.id,
                JournalEntry.entry_type,
                JournalEntry.description,
                JournalEntry.reverses_entry_id,
                JournalEntry.created_at,
            ).where(JournalEntry.id.in_(entry_ids))
        )
    ).all()
    lines = (
        await session.execute(
            select(
                EntryLine.entry_id, EntryLine.account_id, EntryLine.amount_minor, EntryLine.currency
            )
            .where(EntryLine.entry_id.in_(entry_ids))
            .order_by(EntryLine.id)
        )
    ).all()
    by_entry: dict[int, list[LineView]] = {}
    for ln in lines:
        by_entry.setdefault(ln.entry_id, []).append(
            LineView(ln.account_id, ln.amount_minor, ln.currency)
        )
    views = {
        h.id: EntryView(
            h.id,
            h.entry_type,
            h.description,
            h.reverses_entry_id,
            h.created_at,
            tuple(by_entry.get(h.id, [])),
        )
        for h in headers
    }
    return [views[i] for i in entry_ids if i in views]


async def get_entry(session: AsyncSession, entry_id: int) -> EntryView | None:
    found = await _load_entries(session, [entry_id])
    return found[0] if found else None


async def list_entries(
    session: AsyncSession, account_id: int, limit: int = 50, before_id: int | None = None
) -> list[EntryView]:
    """Entries touching ``account_id``, newest first; keyset-paginate with ``before_id``."""
    exists = await session.scalar(select(Account.id).where(Account.id == account_id))
    if exists is None:
        raise UnknownAccount([account_id])
    stmt = (
        select(EntryLine.entry_id)
        .where(EntryLine.account_id == account_id)
        .distinct()
        .order_by(EntryLine.entry_id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(EntryLine.entry_id < before_id)
    ids = list((await session.execute(stmt)).scalars())
    return await _load_entries(session, ids)
