"""Money value object: integer minor units plus an ISO-4217 currency code."""

from __future__ import annotations

from dataclasses import dataclass

from ledgercore.domain.errors import CurrencyMismatch, InvalidAmount, InvalidCurrency


def validate_currency(code: object) -> str:
    """Return ``code`` if it is exactly three upper-case ASCII letters, else raise."""
    if not (
        isinstance(code, str)
        and len(code) == 3
        and code.isascii()
        and code.isalpha()
        and code.isupper()
    ):
        raise InvalidCurrency(f"currency must be 3 upper-case ASCII letters, got {code!r}")
    return code


def validate_minor_units(amount: object) -> int:
    """Return ``amount`` if it is a real ``int`` (bools and floats rejected), else raise."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidAmount(f"amount must be an int of minor units, got {type(amount).__name__}")
    return amount


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in minor units (e.g. cents). Never rounds."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        validate_minor_units(self.amount_minor)
        validate_currency(self.currency)

    def _check_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} with {other.currency}")

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    @property
    def is_positive(self) -> bool:
        return self.amount_minor > 0
