"""Typed errors raised by the posting layer."""


class LedgerError(Exception):
    """Base class for all ledger-domain errors raised by the DB layer."""


class UnknownAccount(LedgerError):  # noqa: N818 - domain vocabulary, not a generic "Error"
    def __init__(self, account_ids: list[int]) -> None:
        self.account_ids = account_ids
        super().__init__(f"unknown account(s): {account_ids}")


class UnknownEntry(LedgerError):  # noqa: N818
    def __init__(self, entry_id: int) -> None:
        self.entry_id = entry_id
        super().__init__(f"unknown journal entry: {entry_id}")


class CurrencyMismatch(LedgerError):  # noqa: N818
    def __init__(self, account_id: int, account_currency: str, line_currency: str) -> None:
        self.account_id = account_id
        self.account_currency = account_currency
        self.line_currency = line_currency
        super().__init__(f"account {account_id} is {account_currency} but line is {line_currency}")


class UnbalancedEntry(LedgerError):  # noqa: N818
    """Entry is structurally invalid: <2 lines, zero amount, or per-currency sum != 0."""


class InsufficientFunds(LedgerError):  # noqa: N818
    def __init__(self, account_id: int, balance_minor: int, delta_minor: int) -> None:
        self.account_id = account_id
        self.balance_minor = balance_minor
        self.delta_minor = delta_minor
        super().__init__(
            f"account {account_id} balance {balance_minor} cannot absorb {delta_minor}"
        )


class EntryAlreadyReversed(LedgerError):  # noqa: N818
    def __init__(self, entry_id: int) -> None:
        self.entry_id = entry_id
        super().__init__(f"entry {entry_id} has already been reversed")


class BalanceOverflow(LedgerError):  # noqa: N818
    def __init__(self, account_id: int, balance_minor: int, delta_minor: int) -> None:
        self.account_id = account_id
        self.balance_minor = balance_minor
        self.delta_minor = delta_minor
        super().__init__(
            f"account {account_id} balance {balance_minor} + {delta_minor} exceeds BIGINT range"
        )
