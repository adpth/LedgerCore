"""FakePayoutProvider: scripted outcomes and submit idempotency by payout_id."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgercore.domain import (
    FakeBehavior,
    FakePayoutProvider,
    Money,
    PayoutProvider,
    ProviderOutcome,
)

AMOUNT = Money(500, "USD")


def test_fake_satisfies_protocol() -> None:
    provider: PayoutProvider = FakePayoutProvider()
    assert provider is not None


async def test_default_succeeds_and_records_calls() -> None:
    p = FakePayoutProvider()
    r = await p.submit("a", AMOUNT, "dest")
    assert r.outcome is ProviderOutcome.SUCCEEDED and r.reference == "fake-a"
    assert p.submit_calls == [("a", AMOUNT, "dest")]
    assert [m.payout_id for m in p.money_sent] == ["a"]
    assert (await p.get_status("a")).outcome is ProviderOutcome.SUCCEEDED
    assert p.status_calls == ["a"]


async def test_fail_is_sticky_and_pays_nothing() -> None:
    p = FakePayoutProvider()
    p.script("a", FakeBehavior.FAIL)
    assert (await p.submit("a", AMOUNT, "d")).outcome is ProviderOutcome.FAILED
    p.script("a", FakeBehavior.SUCCEED)  # a definitive failure is final for this id
    assert (await p.submit("a", AMOUNT, "d")).outcome is ProviderOutcome.FAILED
    assert (await p.get_status("a")).outcome is ProviderOutcome.FAILED
    assert p.money_sent == []


async def test_timeout_after_success_paid_but_unknown() -> None:
    p = FakePayoutProvider()
    p.script("a", FakeBehavior.TIMEOUT_AFTER_SUCCESS)
    assert (await p.submit("a", AMOUNT, "d")).outcome is ProviderOutcome.UNKNOWN
    assert len(p.money_sent) == 1
    assert (await p.get_status("a")).outcome is ProviderOutcome.SUCCEEDED
    retry = await p.submit("a", AMOUNT, "d")
    assert retry.outcome is ProviderOutcome.SUCCEEDED and retry.reference == "fake-a"
    assert len(p.money_sent) == 1


async def test_timeout_without_paying_then_retry_pays_once() -> None:
    p = FakePayoutProvider()
    p.script("a", FakeBehavior.TIMEOUT_WITHOUT_PAYING)
    assert (await p.submit("a", AMOUNT, "d")).outcome is ProviderOutcome.UNKNOWN
    assert (await p.get_status("a")).outcome is ProviderOutcome.UNKNOWN
    assert p.money_sent == []
    p.script("a", FakeBehavior.SUCCEED)
    assert (await p.submit("a", AMOUNT, "d")).outcome is ProviderOutcome.SUCCEEDED
    assert len(p.money_sent) == 1


async def test_raise_pays_nothing() -> None:
    p = FakePayoutProvider()
    p.script("a", FakeBehavior.RAISE)
    with pytest.raises(ConnectionError):
        await p.submit("a", AMOUNT, "d")
    assert p.money_sent == []
    assert len(p.submit_calls) == 1


@given(
    st.lists(
        st.tuples(st.sampled_from(["a", "b", "c"]), st.sampled_from(list(FakeBehavior))),
        max_size=30,
    )
)
def test_never_double_pays_same_id(steps: list[tuple[str, FakeBehavior]]) -> None:
    p = FakePayoutProvider()
    for payout_id, behavior in steps:
        p.script(payout_id, behavior)
        try:
            r = asyncio.run(p.submit(payout_id, AMOUNT, "d"))
        except ConnectionError:
            continue
        if r.outcome is ProviderOutcome.SUCCEEDED:
            assert payout_id in p.payments
    paid_ids = [m.payout_id for m in p.money_sent]
    assert len(paid_ids) == len(set(paid_ids))
    assert not (set(paid_ids) & set(p.failed))
