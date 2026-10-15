"""Request/response models. Money is integer minor units + ISO-4217 code, strictly typed:
floats (even ``10.0``), numeric strings and booleans are rejected for amounts and ids."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from ledgercore.services.ledger import AccountView, BalanceView, EntryView
from ledgercore.services.payouts import PayoutView

BIGINT_MAX = 2**63 - 1

Currency = Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$", examples=["USD"])]
AmountMinor = Annotated[StrictInt, Field(gt=0, le=BIGINT_MAX, examples=[1250])]
AccountId = Annotated[StrictInt, Field(gt=0, le=BIGINT_MAX)]
# NUL is rejected here because Postgres TEXT/JSONB cannot store it.
Description = Annotated[StrictStr, Field(max_length=500, pattern=r"^[^\x00]*$")]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OpenAccountRequest(_Request):
    currency: Currency


class DepositRequest(_Request):
    account_id: AccountId
    amount_minor: AmountMinor
    currency: Currency
    description: Description = ""


class WithdrawalRequest(DepositRequest):
    pass


class TransferRequest(_Request):
    from_account_id: AccountId
    to_account_id: AccountId
    amount_minor: AmountMinor
    currency: Currency
    description: Description = ""


Destination = Annotated[StrictStr, Field(min_length=1, max_length=200, pattern=r"^[^\x00]+$")]


class PayoutRequest(_Request):
    account_id: AccountId
    amount_minor: AmountMinor
    currency: Currency
    destination: Destination


class ResolvePayoutRequest(_Request):
    outcome: Literal["succeeded", "failed"]


class ReverseRequest(_Request):
    description: Description | None = None


# ---- responses ----------------------------------------------------------------------


class AccountOut(BaseModel):
    id: int
    currency: str
    allow_negative: bool
    is_system: bool
    created_at: datetime

    @classmethod
    def of(cls, v: AccountView) -> AccountOut:
        return cls(
            id=v.id,
            currency=v.currency,
            allow_negative=v.allow_negative,
            is_system=v.is_system,
            created_at=v.created_at,
        )


class BalanceOut(BaseModel):
    account_id: int
    currency: str
    balance_minor: int

    @classmethod
    def of(cls, v: BalanceView) -> BalanceOut:
        return cls(account_id=v.account_id, currency=v.currency, balance_minor=v.balance_minor)


class LineOut(BaseModel):
    account_id: int
    amount_minor: int
    currency: str


class EntryOut(BaseModel):
    id: int
    entry_type: str
    description: str | None
    reverses_entry_id: int | None
    created_at: datetime
    lines: list[LineOut]

    @classmethod
    def of(cls, v: EntryView) -> EntryOut:
        return cls(
            id=v.id,
            entry_type=v.entry_type,
            description=v.description,
            reverses_entry_id=v.reverses_entry_id,
            created_at=v.created_at,
            lines=[
                LineOut(
                    account_id=ln.account_id, amount_minor=ln.amount_minor, currency=ln.currency
                )
                for ln in v.lines
            ],
        )


class EntryPage(BaseModel):
    account_id: int
    entries: list[EntryOut]
    next_before_id: int | None


class PayoutOut(BaseModel):
    id: str
    account_id: int
    amount_minor: int
    currency: str
    destination: str
    status: str
    hold_entry_id: int | None
    final_entry_id: int | None
    release_entry_id: int | None
    provider_reference: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, v: PayoutView) -> PayoutOut:
        return cls(
            id=v.id,
            account_id=v.account_id,
            amount_minor=v.amount_minor,
            currency=v.currency,
            destination=v.destination,
            status=v.status.value,
            hold_entry_id=v.hold_entry_id,
            final_entry_id=v.final_entry_id,
            release_entry_id=v.release_entry_id,
            provider_reference=v.provider_reference,
            failure_reason=v.failure_reason,
            created_at=v.created_at,
            updated_at=v.updated_at,
        )
