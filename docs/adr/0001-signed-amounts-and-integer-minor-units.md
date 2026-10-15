# ADR 0001: Signed amounts and integer minor units

Status: Accepted

## Context
A double-entry ledger needs one unambiguous way to represent direction and quantity.
Floating point and decimals-as-strings invite rounding and parsing bugs; separate
debit/credit columns invite rows that set both or neither.

## Decision
- Every `entry_lines.amount_minor` is a signed `BIGINT` in currency minor units (cents for
  USD). Debit is positive, credit is negative. A transfer A -> B of X is A: -X, B: +X.
- An account balance is `SUM(amount_minor)` of its lines. An entry is valid when its lines
  sum to zero per currency.
- Zero-amount lines are rejected (`CHECK amount_minor <> 0`). Python rejects `bool` and
  non-`int` amounts (`validate_minor_units`); the API uses `StrictInt` with `0 < x <= 2^63-1`.
- Currency is a 3-letter upper-case code; a composite FK `(account_id, currency)` to
  `accounts(id, currency)` ties each line to its account's currency.

## Consequences
- Arithmetic is exact; the balance invariant is a plain sum, easy to check in SQL.
- Minor-unit scale is the client's responsibility; the ledger does not know that JPY has
  0 decimals or BHD has 3.
- Amounts and balances are bounded by `BIGINT`. `post_entry` checks the new balance before
  writing it and rejects an overflow as `BalanceOverflow` (HTTP 422 `balance_overflow`).
