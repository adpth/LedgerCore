"""Double-entry ``Line`` and ``Entry`` aggregate.

Sign convention: a line's ``amount_minor`` is positive for a debit and negative for a
credit. An account's balance is the plain sum of its lines' amounts, so a deposit
*increases* the target account (positive line) and decreases the external/settlement
account (negative line).

External-payout entries (see ``ledgercore.domain.payout`` for the saga):

* ``HOLD``          customer -> holds account. Reserves funds while the provider is called.
* ``HOLD_RELEASE``  the exact negation of a HOLD. It *is* a reversal of that hold, so like
  ``REVERSAL`` it must carry ``reverses_entry_id`` (the hold's id). Reusing the column means
  the DB's ``UNIQUE(reverses_entry_id)`` guarantees a hold is released (or reversed) at
  most once. It keeps its own type so the history says *why* the hold was undone.
* ``PAYOUT``        holds account -> external clearing account: the final settlement once
  the provider has definitively paid. It does not reference the hold; the hold is consumed
  because the holds account is debited back by exactly the held amount.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ledgercore.domain.errors import (
    CurrencyMismatch,
    InvalidAmount,
    InvalidEntry,
    SameAccountTransfer,
    UnbalancedEntry,
)
from ledgercore.domain.money import Money, validate_currency, validate_minor_units

MIN_LINES = 2


class EntryType(enum.StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER = "TRANSFER"
    REVERSAL = "REVERSAL"
    HOLD = "HOLD"
    HOLD_RELEASE = "HOLD_RELEASE"
    PAYOUT = "PAYOUT"


# Entry types that undo an earlier entry and must name it via ``reverses_entry_id``.
REVERSING_TYPES = frozenset({EntryType.REVERSAL, EntryType.HOLD_RELEASE})


def _validate_account_id(account_id: object) -> int:
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        raise InvalidEntry(f"account_id must be a positive int, got {account_id!r}")
    return account_id


@dataclass(frozen=True, slots=True)
class Line:
    """One posting: signed minor units against one account. Debit > 0, credit < 0."""

    account_id: int
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        _validate_account_id(self.account_id)
        validate_minor_units(self.amount_minor)
        if self.amount_minor == 0:
            raise InvalidAmount("line amount must be nonzero")
        validate_currency(self.currency)

    @property
    def money(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def is_debit(self) -> bool:
        return self.amount_minor > 0

    def negated(self) -> Line:
        return Line(self.account_id, -self.amount_minor, self.currency)


def _check_invariants(lines: Sequence[Line]) -> None:
    if len(lines) < MIN_LINES:
        raise InvalidEntry(f"entry needs at least {MIN_LINES} lines, got {len(lines)}")
    totals: dict[str, int] = {}
    for line in lines:
        if not isinstance(line, Line):
            raise InvalidEntry(f"entry lines must be Line instances, got {type(line).__name__}")
        totals[line.currency] = totals.get(line.currency, 0) + line.amount_minor
    if len(totals) > 1:
        raise CurrencyMismatch(f"entry mixes currencies: {sorted(totals)}")
    unbalanced = {cur: total for cur, total in totals.items() if total != 0}
    if unbalanced:
        raise UnbalancedEntry(f"lines do not sum to zero per currency: {unbalanced}")


@dataclass(frozen=True, slots=True, init=False)
class Entry:
    """A balanced journal entry. Invariants are enforced at construction."""

    entry_type: EntryType
    lines: tuple[Line, ...]
    description: str = ""
    reverses_entry_id: int | None = None

    def __init__(
        self,
        entry_type: EntryType,
        lines: Iterable[Line],
        description: str = "",
        reverses_entry_id: int | None = None,
    ) -> None:
        object.__setattr__(self, "entry_type", EntryType(entry_type))
        object.__setattr__(self, "lines", tuple(lines))
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "reverses_entry_id", reverses_entry_id)
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.description, str):
            raise InvalidEntry("description must be a str")
        if self.entry_type in REVERSING_TYPES:
            if self.reverses_entry_id is None:
                raise InvalidEntry(f"{self.entry_type} entry requires reverses_entry_id")
            _validate_account_id(self.reverses_entry_id)
        elif self.reverses_entry_id is not None:
            raise InvalidEntry("only REVERSAL/HOLD_RELEASE entries may set reverses_entry_id")
        _check_invariants(self.lines)

    @property
    def currency(self) -> str:
        """The entry's currency (first line's currency; unique unless multi-currency is on)."""
        return self.lines[0].currency

    # ---- constructors -------------------------------------------------------------

    @staticmethod
    def _require_positive(amount: Money) -> None:
        if not isinstance(amount, Money):
            raise InvalidAmount(f"amount must be Money, got {type(amount).__name__}")
        if not amount.is_positive:
            raise InvalidAmount(f"amount must be positive, got {amount.amount_minor}")

    @classmethod
    def _two_leg(
        cls,
        entry_type: EntryType,
        debit_account_id: int,
        credit_account_id: int,
        amount: Money,
        description: str,
    ) -> Entry:
        cls._require_positive(amount)
        if debit_account_id == credit_account_id:
            err = SameAccountTransfer if entry_type is EntryType.TRANSFER else InvalidEntry
            raise err(f"{entry_type} cannot debit and credit the same account")
        return cls(
            entry_type,
            (
                Line(credit_account_id, -amount.amount_minor, amount.currency),
                Line(debit_account_id, amount.amount_minor, amount.currency),
            ),
            description,
        )

    @classmethod
    def deposit(
        cls, external_account_id: int, account_id: int, amount: Money, description: str = ""
    ) -> Entry:
        """Move ``amount`` from an external/settlement account into ``account_id``."""
        return cls._two_leg(EntryType.DEPOSIT, account_id, external_account_id, amount, description)

    @classmethod
    def withdrawal(
        cls, account_id: int, external_account_id: int, amount: Money, description: str = ""
    ) -> Entry:
        """Move ``amount`` out of ``account_id`` to an external/settlement account."""
        return cls._two_leg(
            EntryType.WITHDRAWAL, external_account_id, account_id, amount, description
        )

    @classmethod
    def transfer(
        cls, src_account_id: int, dst_account_id: int, amount: Money, description: str = ""
    ) -> Entry:
        """Move ``amount`` from ``src_account_id`` to ``dst_account_id``."""
        return cls._two_leg(EntryType.TRANSFER, dst_account_id, src_account_id, amount, description)

    @classmethod
    def reversal_of(
        cls,
        original_lines: Iterable[Line],
        original_entry_id: int,
        description: str | None = None,
    ) -> Entry:
        """Build a REVERSAL whose lines are exactly the negation of ``original_lines``."""
        return cls(
            EntryType.REVERSAL,
            (line.negated() for line in original_lines),
            description if description is not None else f"Reversal of entry {original_entry_id}",
            reverses_entry_id=original_entry_id,
        )

    @classmethod
    def hold(
        cls, account_id: int, holds_account_id: int, amount: Money, description: str = ""
    ) -> Entry:
        """Reserve ``amount``: move it from ``account_id`` into the holds account."""
        return cls._two_leg(EntryType.HOLD, holds_account_id, account_id, amount, description)

    @classmethod
    def hold_release(
        cls,
        account_id: int,
        holds_account_id: int,
        amount: Money,
        reverses_entry_id: int,
        description: str = "",
    ) -> Entry:
        """Undo the HOLD ``reverses_entry_id``: its exact negation (holds -> ``account_id``)."""
        hold = cls.hold(account_id, holds_account_id, amount)
        return cls(
            EntryType.HOLD_RELEASE,
            (line.negated() for line in hold.lines),
            description,
            reverses_entry_id=reverses_entry_id,
        )

    @classmethod
    def payout(
        cls, holds_account_id: int, clearing_account_id: int, amount: Money, description: str = ""
    ) -> Entry:
        """Settle a held payout: move ``amount`` from the holds account to external clearing."""
        return cls._two_leg(
            EntryType.PAYOUT, clearing_account_id, holds_account_id, amount, description
        )
