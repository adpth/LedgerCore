"""Hypothesis property tests for the ledger domain invariants."""

from __future__ import annotations

import random
from collections import defaultdict

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ledgercore.domain import (
    Entry,
    EntryType,
    InvalidAmount,
    InvalidEntry,
    Line,
    Money,
    UnbalancedEntry,
    request_fingerprint,
)

currencies = st.sampled_from(["USD", "EUR", "JPY", "GBP"])
account_ids = st.integers(min_value=1, max_value=10**12)
positive_amounts = st.integers(min_value=1, max_value=10**18)
moneys = st.builds(Money, positive_amounts, currencies)


def per_currency_totals(lines: tuple[Line, ...]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for line in lines:
        totals[line.currency] += line.amount_minor
    return dict(totals)


@st.composite
def transfers(draw: st.DrawFn) -> Entry:
    src = draw(account_ids)
    dst = draw(account_ids.filter(lambda a: a != src))
    return Entry.transfer(src, dst, draw(moneys))


@st.composite
def balanced_entries(draw: st.DrawFn) -> Entry:
    """Arbitrary N-line single-currency balanced entries (plus constructor-built ones)."""
    kind = draw(st.sampled_from(["transfer", "deposit", "withdrawal", "split"]))
    a = draw(account_ids)
    b = draw(account_ids.filter(lambda x: x != a))
    amount = draw(moneys)
    if kind == "transfer":
        return Entry.transfer(a, b, amount)
    if kind == "deposit":
        return Entry.deposit(a, b, amount)
    if kind == "withdrawal":
        return Entry.withdrawal(a, b, amount)
    debits = draw(st.lists(st.tuples(account_ids, positive_amounts), min_size=1, max_size=6))
    currency = amount.currency
    lines = [Line(acct, amt, currency) for acct, amt in debits]
    lines.append(Line(a, -sum(amt for _, amt in debits), currency))
    order = draw(st.permutations(lines))
    return Entry(EntryType.TRANSFER, order)


@given(transfers())
def test_random_valid_transfers_always_balance(e: Entry) -> None:
    assert len(e.lines) == 2
    assert all(v == 0 for v in per_currency_totals(e.lines).values())
    assert {line.amount_minor > 0 for line in e.lines} == {True, False}


@given(account_ids, account_ids, st.integers(max_value=0), currencies)
def test_non_positive_transfers_always_rejected(src: int, dst: int, amt: int, cur: str) -> None:
    assume(src != dst)
    with pytest.raises(InvalidAmount):
        Entry.transfer(src, dst, Money(amt, cur))


@given(balanced_entries(), st.integers(min_value=1, max_value=2**63 - 1))
def test_reversal_of_any_entry_nets_to_zero(e: Entry, original_id: int) -> None:
    rev = Entry.reversal_of(e.lines, original_id)
    assert rev.reverses_entry_id == original_id
    assert len(rev.lines) == len(e.lines)
    net: dict[tuple[int, str], int] = defaultdict(int)
    for line in e.lines + rev.lines:
        net[(line.account_id, line.currency)] += line.amount_minor
    assert all(v == 0 for v in net.values())
    for orig, r in zip(e.lines, rev.lines, strict=True):
        assert r == Line(orig.account_id, -orig.amount_minor, orig.currency)
    # Reversing the reversal yields the original lines.
    assert Entry.reversal_of(rev.lines, 1).lines == e.lines


@given(balanced_entries(), st.data())
def test_mutated_line_breaks_invariant(e: Entry, data: st.DataObject) -> None:
    idx = data.draw(st.integers(min_value=0, max_value=len(e.lines) - 1))
    delta = data.draw(st.integers(min_value=-(10**6), max_value=10**6).filter(lambda d: d != 0))
    target = e.lines[idx]
    assume(target.amount_minor + delta != 0)
    mutated = list(e.lines)
    mutated[idx] = Line(target.account_id, target.amount_minor + delta, target.currency)
    with pytest.raises(UnbalancedEntry):
        Entry(e.entry_type, mutated)


@given(balanced_entries(), st.data())
def test_dropping_a_line_breaks_invariant(e: Entry, data: st.DataObject) -> None:
    idx = data.draw(st.integers(min_value=0, max_value=len(e.lines) - 1))
    remaining = e.lines[:idx] + e.lines[idx + 1 :]
    # Fewer than two lines -> InvalidEntry; otherwise the remainder is unbalanced.
    with pytest.raises(InvalidEntry):
        Entry(e.entry_type, remaining)


@given(moneys, moneys)
def test_money_add_sub_roundtrip(a: Money, b: Money) -> None:
    assume(a.currency == b.currency)
    assert (a + b) - b == a
    assert a - a == Money(0, a.currency)


json_scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=10)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=5), children, max_size=4)
    ),
    max_leaves=15,
)
json_bodies = st.dictionaries(st.text(max_size=8), json_values, max_size=6)


@given(json_bodies, st.randoms())
def test_fingerprint_ignores_key_order(body: dict[str, object], rnd: random.Random) -> None:
    items = list(body.items())
    rnd.shuffle(items)
    assert request_fingerprint("POST", "/x", body) == request_fingerprint("POST", "/x", dict(items))


@given(json_bodies, st.text(min_size=1, max_size=8), json_values)
def test_fingerprint_changes_when_a_value_changes(
    body: dict[str, object], key: str, new_value: object
) -> None:
    changed = {**body, key: new_value}
    assume(changed != body)
    assert request_fingerprint("POST", "/x", body) != request_fingerprint("POST", "/x", changed)
