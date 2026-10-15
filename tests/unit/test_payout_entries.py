"""HOLD / HOLD_RELEASE / PAYOUT entry constructors."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ledgercore.domain import Entry, EntryType, InvalidAmount, InvalidEntry, Line, Money

USD = "USD"
CUSTOMER, HOLDS, CLEARING = 1, 2, 3

account_ids = st.integers(min_value=1, max_value=10**12)
moneys = st.builds(
    Money, st.integers(min_value=1, max_value=10**18), st.sampled_from(["USD", "EUR", "JPY"])
)


def net_by_account(*entries: Entry) -> dict[int, int]:
    net: dict[int, int] = defaultdict(int)
    for e in entries:
        for line in e.lines:
            net[line.account_id] += line.amount_minor
    return dict(net)


def test_hold_moves_customer_to_holds() -> None:
    e = Entry.hold(CUSTOMER, HOLDS, Money(500, USD))
    assert e.entry_type is EntryType.HOLD
    assert set(e.lines) == {Line(CUSTOMER, -500, USD), Line(HOLDS, 500, USD)}
    assert e.reverses_entry_id is None


def test_hold_release_is_exact_negation_and_links_the_hold() -> None:
    hold = Entry.hold(CUSTOMER, HOLDS, Money(500, USD))
    rel = Entry.hold_release(CUSTOMER, HOLDS, Money(500, USD), reverses_entry_id=7)
    assert rel.entry_type is EntryType.HOLD_RELEASE
    assert rel.reverses_entry_id == 7
    assert rel.lines == tuple(line.negated() for line in hold.lines)


def test_payout_moves_holds_to_clearing() -> None:
    e = Entry.payout(HOLDS, CLEARING, Money(500, USD))
    assert e.entry_type is EntryType.PAYOUT
    assert set(e.lines) == {Line(HOLDS, -500, USD), Line(CLEARING, 500, USD)}
    assert e.reverses_entry_id is None


def test_hold_release_requires_valid_reverses_entry_id() -> None:
    lines = [Line(HOLDS, -1, USD), Line(CUSTOMER, 1, USD)]
    with pytest.raises(InvalidEntry):
        Entry(EntryType.HOLD_RELEASE, lines)
    with pytest.raises(InvalidEntry):
        Entry.hold_release(CUSTOMER, HOLDS, Money(1, USD), reverses_entry_id=0)
    for t in (EntryType.HOLD, EntryType.PAYOUT):
        with pytest.raises(InvalidEntry):
            Entry(t, lines, reverses_entry_id=5)


@pytest.mark.parametrize(
    "factory",
    [
        lambda m: Entry.hold(CUSTOMER, HOLDS, m),
        lambda m: Entry.hold_release(CUSTOMER, HOLDS, m, 1),
        lambda m: Entry.payout(HOLDS, CLEARING, m),
    ],
)
@pytest.mark.parametrize("amount", [0, -1, None])
def test_payout_constructors_reject_bad_amounts(factory: Any, amount: int | None) -> None:
    with pytest.raises(InvalidAmount):
        factory(Money(amount, USD) if amount is not None else 5)


def test_payout_constructors_reject_same_account() -> None:
    m = Money(1, USD)
    with pytest.raises(InvalidEntry):
        Entry.hold(1, 1, m)
    with pytest.raises(InvalidEntry):
        Entry.hold_release(1, 1, m, 9)
    with pytest.raises(InvalidEntry):
        Entry.payout(1, 1, m)


@given(account_ids, account_ids, moneys, st.integers(min_value=1, max_value=2**63 - 1))
def test_hold_then_release_nets_to_zero_per_account(
    customer: int, holds: int, amount: Money, hold_id: int
) -> None:
    assume(customer != holds)
    hold = Entry.hold(customer, holds, amount)
    rel = Entry.hold_release(customer, holds, amount, hold_id)
    assert net_by_account(hold, rel) == {customer: 0, holds: 0}


@given(st.lists(account_ids, min_size=3, max_size=3, unique=True), moneys)
def test_hold_then_payout_moves_exactly_amount_customer_to_clearing(
    accounts: list[int], amount: Money
) -> None:
    customer, holds, clearing = accounts
    net = net_by_account(Entry.hold(customer, holds, amount), Entry.payout(holds, clearing, amount))
    assert net == {customer: -amount.amount_minor, holds: 0, clearing: amount.amount_minor}
