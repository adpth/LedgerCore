from __future__ import annotations

from typing import Any

import pytest

from ledgercore.domain import CurrencyMismatch, InvalidAmount, InvalidCurrency, Money


def test_money_holds_minor_units_and_currency() -> None:
    m = Money(1234, "USD")
    assert m.amount_minor == 1234
    assert m.currency == "USD"


def test_money_is_frozen_and_hashable() -> None:
    m = Money(1, "USD")
    with pytest.raises(AttributeError):
        m.amount_minor = 2  # type: ignore[misc]
    assert {m, Money(1, "USD")} == {m}


@pytest.mark.parametrize("bad", [1.0, 0.5, True, False, "10", None, 10**0 * 1.5])
def test_money_rejects_non_int_amounts(bad: Any) -> None:
    with pytest.raises(InvalidAmount):
        Money(bad, "USD")


@pytest.mark.parametrize("bad", ["usd", "US", "USDT", "U$D", "12A", "", "ÄBC", None, 840])
def test_money_rejects_bad_currency(bad: Any) -> None:
    with pytest.raises(InvalidCurrency):
        Money(1, bad)


def test_add_and_sub_same_currency() -> None:
    assert Money(150, "EUR") + Money(50, "EUR") == Money(200, "EUR")
    assert Money(150, "EUR") - Money(200, "EUR") == Money(-50, "EUR")
    assert -Money(7, "EUR") == Money(-7, "EUR")


def test_add_sub_reject_currency_mismatch() -> None:
    with pytest.raises(CurrencyMismatch):
        _ = Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(CurrencyMismatch):
        _ = Money(1, "USD") - Money(1, "EUR")


def test_add_with_non_money_is_type_error() -> None:
    with pytest.raises(TypeError):
        _ = Money(1, "USD") + 1  # type: ignore[operator]


def test_large_values_are_exact() -> None:
    big = 10**30
    assert (Money(big, "JPY") + Money(1, "JPY")).amount_minor == big + 1


def test_zero_and_negative_money_allowed_but_not_positive() -> None:
    assert not Money(0, "USD").is_positive
    assert not Money(-1, "USD").is_positive
    assert Money(1, "USD").is_positive


def test_error_codes_are_stable() -> None:
    assert InvalidAmount.code == "invalid_amount"
    assert InvalidCurrency.code == "invalid_currency"
    assert CurrencyMismatch.code == "currency_mismatch"
