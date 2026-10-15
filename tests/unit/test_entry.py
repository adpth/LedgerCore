from __future__ import annotations

from typing import Any

import pytest

from ledgercore.domain import (
    CurrencyMismatch,
    DomainError,
    Entry,
    EntryType,
    InvalidAmount,
    InvalidCurrency,
    InvalidEntry,
    Line,
    Money,
    SameAccountTransfer,
    UnbalancedEntry,
)

USD = "USD"


def usd(n: int) -> Money:
    return Money(n, USD)


# ---- Line ---------------------------------------------------------------------------


def test_line_debit_positive_credit_negative() -> None:
    assert Line(1, 10, USD).is_debit
    assert not Line(1, -10, USD).is_debit
    assert Line(1, -10, USD).money == Money(-10, USD)


def test_line_rejects_zero_amount() -> None:
    with pytest.raises(InvalidAmount):
        Line(1, 0, USD)


@pytest.mark.parametrize("bad", [1.0, True, "5"])
def test_line_rejects_non_int_amount(bad: Any) -> None:
    with pytest.raises(InvalidAmount):
        Line(1, bad, USD)


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1", None])
def test_line_rejects_bad_account_id(bad: Any) -> None:
    with pytest.raises(InvalidEntry):
        Line(bad, 1, USD)


def test_line_rejects_bad_currency() -> None:
    with pytest.raises(InvalidCurrency):
        Line(1, 1, "usd")


def test_line_negated() -> None:
    assert Line(3, 5, USD).negated() == Line(3, -5, USD)


# ---- Entry invariants -----------------------------------------------------------------


def test_balanced_entry_constructs_and_freezes_lines_as_tuple() -> None:
    lines = [Line(1, -100, USD), Line(2, 60, USD), Line(3, 40, USD)]
    e = Entry(EntryType.TRANSFER, lines, "split")
    assert e.lines == tuple(lines)
    assert isinstance(e.lines, tuple)
    assert e.currency == USD
    lines.append(Line(4, 1, USD))  # mutating the source list must not affect the entry
    assert len(e.lines) == 3
    with pytest.raises(AttributeError):
        e.description = "x"  # type: ignore[misc]


@pytest.mark.parametrize("lines", [[], [Line(1, 1, USD)]])
def test_entry_requires_at_least_two_lines(lines: list[Line]) -> None:
    with pytest.raises(InvalidEntry):
        Entry(EntryType.TRANSFER, lines)


def test_entry_rejects_unbalanced() -> None:
    with pytest.raises(UnbalancedEntry) as exc:
        Entry(EntryType.TRANSFER, [Line(1, -100, USD), Line(2, 99, USD)])
    assert exc.value.code == "unbalanced_entry"


def test_entry_rejects_multi_currency_by_default() -> None:
    lines = [Line(1, -1, USD), Line(2, 1, USD), Line(3, -1, "EUR"), Line(4, 1, "EUR")]
    with pytest.raises(CurrencyMismatch):
        Entry(EntryType.TRANSFER, lines)


def test_entry_rejects_non_line_objects() -> None:
    with pytest.raises(InvalidEntry):
        Entry(EntryType.TRANSFER, [Line(1, 1, USD), (2, -1, USD)])  # type: ignore[list-item]


def test_reversal_type_requires_reverses_entry_id() -> None:
    lines = [Line(1, 1, USD), Line(2, -1, USD)]
    with pytest.raises(InvalidEntry):
        Entry(EntryType.REVERSAL, lines)
    with pytest.raises(InvalidEntry):
        Entry(EntryType.TRANSFER, lines, reverses_entry_id=5)
    with pytest.raises(InvalidEntry):
        Entry(EntryType.REVERSAL, lines, reverses_entry_id=0)


def test_entry_type_accepts_string_value() -> None:
    e = Entry("TRANSFER", [Line(1, 1, USD), Line(2, -1, USD)])  # type: ignore[arg-type]
    assert e.entry_type is EntryType.TRANSFER


# ---- Constructors ---------------------------------------------------------------------


def test_deposit_increases_account_and_credits_external() -> None:
    e = Entry.deposit(external_account_id=99, account_id=1, amount=usd(500))
    assert e.entry_type is EntryType.DEPOSIT
    assert set(e.lines) == {Line(1, 500, USD), Line(99, -500, USD)}
    assert e.reverses_entry_id is None


def test_withdrawal_decreases_account() -> None:
    e = Entry.withdrawal(account_id=1, external_account_id=99, amount=usd(500))
    assert e.entry_type is EntryType.WITHDRAWAL
    assert set(e.lines) == {Line(1, -500, USD), Line(99, 500, USD)}


def test_transfer_moves_from_src_to_dst() -> None:
    e = Entry.transfer(1, 2, usd(250), description="rent")
    assert e.entry_type is EntryType.TRANSFER
    assert set(e.lines) == {Line(1, -250, USD), Line(2, 250, USD)}
    assert e.description == "rent"


def test_transfer_rejects_same_account() -> None:
    with pytest.raises(SameAccountTransfer) as exc:
        Entry.transfer(1, 1, usd(1))
    assert exc.value.code == "same_account_transfer"
    assert isinstance(exc.value, InvalidEntry)


@pytest.mark.parametrize(
    "factory",
    [
        lambda m: Entry.deposit(9, 1, m),
        lambda m: Entry.withdrawal(1, 9, m),
        lambda m: Entry.transfer(1, 2, m),
    ],
)
@pytest.mark.parametrize("amount", [0, -1, -100])
def test_constructors_reject_non_positive_amounts(factory: Any, amount: int) -> None:
    with pytest.raises(InvalidAmount):
        factory(usd(amount))


def test_constructors_reject_non_money_amount() -> None:
    with pytest.raises(InvalidAmount):
        Entry.transfer(1, 2, 100)  # type: ignore[arg-type]


def test_deposit_and_withdrawal_reject_same_account() -> None:
    with pytest.raises(InvalidEntry):
        Entry.deposit(1, 1, usd(1))
    with pytest.raises(InvalidEntry):
        Entry.withdrawal(1, 1, usd(1))


def test_reversal_of_negates_lines_exactly() -> None:
    original = Entry.transfer(1, 2, usd(250))
    rev = Entry.reversal_of(original.lines, original_entry_id=42)
    assert rev.entry_type is EntryType.REVERSAL
    assert rev.reverses_entry_id == 42
    assert rev.lines == tuple(Line(ln.account_id, -ln.amount_minor, USD) for ln in original.lines)
    assert rev.description == "Reversal of entry 42"
    assert Entry.reversal_of(original.lines, 42, "oops").description == "oops"


def test_reversal_of_empty_or_unbalanced_lines_is_rejected() -> None:
    with pytest.raises(InvalidEntry):
        Entry.reversal_of([], 1)
    with pytest.raises(UnbalancedEntry):
        Entry.reversal_of([Line(1, 5, USD), Line(2, -4, USD)], 1)


# ---- Error hierarchy ------------------------------------------------------------------


def test_error_hierarchy_and_codes() -> None:
    expected = {
        DomainError: "domain_error",
        InvalidAmount: "invalid_amount",
        InvalidCurrency: "invalid_currency",
        CurrencyMismatch: "currency_mismatch",
        InvalidEntry: "invalid_entry",
        UnbalancedEntry: "unbalanced_entry",
        SameAccountTransfer: "same_account_transfer",
    }
    for cls, code in expected.items():
        assert issubclass(cls, DomainError)
        assert cls.code == code
    assert len(set(expected.values())) == len(expected)
    assert issubclass(UnbalancedEntry, InvalidEntry)
