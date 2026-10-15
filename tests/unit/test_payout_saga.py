"""Payout saga state machine."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgercore.domain import (
    TERMINAL,
    IllegalPayoutTransition,
    InvalidAmount,
    InvalidPayout,
    Money,
    Payout,
    PayoutStatus,
    ProviderOutcome,
    ProviderResult,
    can_transition,
    transition,
)

S = PayoutStatus
ALLOWED = {
    (S.PENDING, S.HELD),
    (S.PENDING, S.REJECTED),
    (S.HELD, S.PROVIDER_PENDING),
    (S.PROVIDER_PENDING, S.COMPLETED),
    (S.PROVIDER_PENDING, S.FAILED),
    (S.PROVIDER_PENDING, S.NEEDS_RECONCILIATION),
    (S.NEEDS_RECONCILIATION, S.COMPLETED),
    (S.NEEDS_RECONCILIATION, S.FAILED),
    (S.FAILED, S.RELEASED),
}


def new_payout() -> Payout:
    return Payout("po-1", 1, Money(500, "USD"), "acct:ext-9")


def in_flight() -> Payout:
    return new_payout().advance(S.HELD).advance(S.PROVIDER_PENDING)


def test_transition_table_is_exactly_the_documented_one() -> None:
    for a, b in itertools.product(S, S):
        assert can_transition(a, b) == ((a, b) in ALLOWED), (a, b)


def test_illegal_transitions_raise_typed_error() -> None:
    for a, b in itertools.product(S, S):
        if (a, b) not in ALLOWED:
            with pytest.raises(IllegalPayoutTransition) as exc:
                transition(a, b)
            assert exc.value.code == "illegal_payout_transition"
        else:
            assert transition(a, b) is b


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert {S.COMPLETED, S.RELEASED, S.REJECTED} == TERMINAL
    for t in TERMINAL:
        assert not any(can_transition(t, b) for b in S)
    assert S.NEEDS_RECONCILIATION not in TERMINAL


def test_only_failed_leads_to_released() -> None:
    assert {a for a in S if can_transition(a, S.RELEASED)} == {S.FAILED}


def test_happy_path_and_advance_returns_new_payout() -> None:
    p = new_payout()
    assert p.status is S.PENDING
    held = p.advance(S.HELD)
    assert p.status is S.PENDING and held.status is S.HELD
    done = held.advance(S.PROVIDER_PENDING).on_provider_result(
        ProviderResult(ProviderOutcome.SUCCEEDED, reference="ref-1")
    )
    assert done.status is S.COMPLETED
    assert done.provider_reference == "ref-1"
    assert done.is_terminal
    with pytest.raises(IllegalPayoutTransition):
        done.advance(S.RELEASED)
    with pytest.raises(AttributeError):
        done.status = S.PENDING  # type: ignore[misc]


def test_definitive_failure_allows_release() -> None:
    failed = in_flight().on_provider_result(ProviderResult(ProviderOutcome.FAILED, reason="nope"))
    assert failed.status is S.FAILED
    assert failed.failure_reason == "nope"
    assert failed.advance(S.RELEASED).status is S.RELEASED


def test_unknown_parks_in_reconciliation_and_cannot_release() -> None:
    p = in_flight().on_provider_result(ProviderResult(ProviderOutcome.UNKNOWN, reason="timeout"))
    assert p.status is S.NEEDS_RECONCILIATION
    assert p.failure_reason is None
    assert p.on_provider_result(ProviderResult(ProviderOutcome.UNKNOWN)) is p
    with pytest.raises(IllegalPayoutTransition):
        p.advance(S.RELEASED)
    assert p.on_provider_result(ProviderResult(ProviderOutcome.SUCCEEDED, "r")).status is (
        S.COMPLETED
    )


def test_provider_result_before_provider_call_is_illegal() -> None:
    held = new_payout().advance(S.HELD)
    for outcome in ProviderOutcome:
        with pytest.raises(IllegalPayoutTransition):
            held.on_provider_result(ProviderResult(outcome))


@pytest.mark.parametrize(
    ("kwargs", "err"),
    [
        ({"id": ""}, InvalidPayout),
        ({"destination": ""}, InvalidPayout),
        ({"account_id": 0}, InvalidPayout),
        ({"amount": Money(0, "USD")}, InvalidAmount),
        ({"amount": 5}, InvalidAmount),
    ],
)
def test_payout_validation(kwargs: dict[str, Any], err: type[Exception]) -> None:
    base: dict[str, Any] = {
        "id": "po",
        "account_id": 1,
        "amount": Money(1, "USD"),
        "destination": "d",
    }
    with pytest.raises(err):
        Payout(**{**base, **kwargs})


@given(st.lists(st.sampled_from(list(ProviderOutcome)), min_size=1, max_size=10))
def test_unknown_can_never_reach_released(outcomes: list[ProviderOutcome]) -> None:
    """Feed arbitrary provider answers, eagerly releasing whenever the table allows it:
    RELEASED is reached only after a definitive FAILED answer was applied."""
    p = in_flight()
    saw_failed = False
    for outcome in outcomes:
        if p.status in (S.PROVIDER_PENDING, S.NEEDS_RECONCILIATION):
            p = p.on_provider_result(ProviderResult(outcome))
            saw_failed |= outcome is ProviderOutcome.FAILED
        if can_transition(p.status, S.RELEASED):
            p = p.advance(S.RELEASED)
        assert p.status is not S.RELEASED or saw_failed
    if all(o is ProviderOutcome.UNKNOWN for o in outcomes):
        assert p.status is S.NEEDS_RECONCILIATION
