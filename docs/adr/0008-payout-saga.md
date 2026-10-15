# ADR 0008: External payout saga (hold, call provider, settle or release)

Status: Accepted

## Context
Paying money out to an external provider cannot be atomic with our database: the provider
call can time out after the money has already left. We need funds reserved while the call is
in flight, a final ledger entry only when the provider has definitively paid, and recovery
from a crash at any point without losing or double-moving money.

## Decision
Accounts: per currency, three system accounts, one per `system_kind` (migration 0004):
`external` (deposits/withdrawals), `holds` and `clearing`. Unique on `(currency, system_kind)`
among system rows; existing system rows became `external`.

Entries (domain/entry.py): `HOLD` customer -> holds; `PAYOUT` holds -> clearing;
`HOLD_RELEASE` the exact negation of the HOLD, carrying `reverses_entry_id`, so the existing
`UNIQUE(reverses_entry_id)` makes a hold releasable at most once.

State machine (domain/payout.py, mirrored by a DB trigger on `payouts`):

```
PENDING -> HELD -> PROVIDER_PENDING -> COMPLETED
   |                    |  \-> FAILED -> RELEASED
   v                    \---> NEEDS_RECONCILIATION -> COMPLETED | FAILED
REJECTED
```
Terminal: COMPLETED, RELEASED, REJECTED. The trigger rejects DELETE, any change to a terminal
row, changes to identity columns or already-set entry links, and status moves off the table.
(The table exists in Python and SQL on purpose; a test enumerates every status pair and
compares the two.)

Steps (`services/payouts.py`), each ONE transaction that locks the payout row
(`FOR UPDATE`), re-checks the expected status, and writes the ledger entry, status change
and outbox event together:
1. `create_payout` (inside the API's idempotency transaction): lock customer and holds
   accounts in ascending id order, insert the payout, post the HOLD, status HELD
   (`payout.held`). Insufficient funds -> REJECTED, nothing held, no event.
2. HELD -> PROVIDER_PENDING, committed before the provider is called.
3. `provider.submit(payout_id, ...)` with no transaction open and no lock held.
4. PROVIDER_PENDING + answer: SUCCEEDED -> PAYOUT entry + COMPLETED (`payout.completed`);
   FAILED -> FAILED (`payout.failed`), then in a separate transaction HOLD_RELEASE +
   RELEASED (`payout.released`); UNKNOWN or an exception -> NEEDS_RECONCILIATION
   (`payout.needs_reconciliation`), funds stay held.

Events are written under the payout row lock, which gives per-aggregate `seq` order as ADR
0007 requires. PROVIDER_PENDING and REJECTED emit no event.

## Safety rule
An UNKNOWN provider outcome never releases the hold. After a timeout the money may already
have left; releasing would pay the customer twice. The only edge into RELEASED is from FAILED,
entered only on a definitive provider FAILED (or an operator verdict). Holding too long is
recoverable; releasing money that was sent is a loss.

## Recovery
`ledgercore-payout-recovery` (`make payout-recovery`) selects payouts not updated for
`--stale-after-s` (default 60) in PENDING, HELD, PROVIDER_PENDING, FAILED or
NEEDS_RECONCILIATION and calls `advance(recovering=True)`:
- PENDING: link an existing HOLD (found by description `payout:<id>:hold`) or post one. Step 1
  normally commits PENDING->HELD atomically, so this covers rows written by other means.
- HELD: continue to PROVIDER_PENDING and the provider.
- PROVIDER_PENDING (crash around the call): `get_status` first; if the provider does not
  know the payout, re-`submit` (idempotent by payout id).
- FAILED: post the release that is owed.
- NEEDS_RECONCILIATION: `get_status` only (never re-submit); a definitive answer completes or
  fails the payout, UNKNOWN leaves it parked.
Crash points covered by tests: after the hold, after PROVIDER_PENDING, after the provider
call before recording, inside the apply transaction (everything rolls back together), after
FAILED before the release.

Operator: `POST /payouts/{id}/resolve {"outcome": "succeeded"|"failed"}` settles a
NEEDS_RECONCILIATION payout. It is state-idempotent (same verdict again is a no-op, the
opposite verdict is 409 `payout_not_resolvable`) and does not need an `Idempotency-Key`.
It is UNAUTHENTICATED until Phase 3, and nothing checks the operator's claim against the
provider.

Concurrent workers: DB-only steps lock with `FOR UPDATE SKIP LOCKED`, so a payout another
worker is mid-transaction on is skipped rather than waited for. Provider calls run with no
lock held, so two workers can call the provider for the same payout; this is safe only
because the provider is idempotent by payout id. The result is applied under a blocking row
lock that re-checks the status, so the ledger entry is posted once.

## API
`POST /payouts` (Idempotency-Key required) commits phase 1 (key claim, payout, hold), then
runs the remaining steps and returns the current payout: 201 for an end state, 202 while in
flight (e.g. NEEDS_RECONCILIATION). A replay of the key returns the current state without
driving the saga again. Insufficient funds is a persisted REJECTED payout returned as 201,
not a 422. If the process dies after phase 1, the client gets no response; retrying the key
returns the in-flight payout and recovery finishes it. `GET /payouts/{id}`. Generic
`POST /entries/{id}/reverse` of HOLD, HOLD_RELEASE and PAYOUT entries is refused with 422
`entry_not_reversible` (a plain REVERSAL of a hold would take its `reverses_entry_id` slot).

## Locking
Order: payout row first, then accounts in ascending id (ADR 0003). `create_payout` is the one
exception, since the payout row does not exist yet: it locks accounts first. It must take the
account locks before inserting the payout, because the insert's foreign key takes a KEY SHARE
lock on the customer account that conflicts with `FOR UPDATE`; two concurrent payouts for one
account otherwise deadlock (found by the concurrency test).

## What is and is not exactly-once
- Ledger effects: each of HOLD, PAYOUT and HOLD_RELEASE is posted at most once per payout
  (row lock + status check; unique `reverses_entry_id` for the release).
- Provider: at-least-once calls, at-most-one payment, and only if the provider honours
  idempotency by payout id. The fake provider does; a real one must be verified.
- Outbox events: at-least-once like every outbox event (ADR 0007); consumers dedupe on
  `event_id`.
- The client response for a request that crashed mid-saga is not exactly-once.

## Limitations
- No real provider adapter. The API defaults to a fake that always succeeds
  (`AlwaysSucceedsLocalProvider`); the recovery CLI uses `FakePayoutProvider`, whose memory
  is per process. A deployment must construct a real provider.
- Resolve is unauthenticated (Phase 3).
- No retry/backoff, attempt counters or alerting for payouts stuck in NEEDS_RECONCILIATION.
- A recovery pass processes payouts sequentially; a slow provider slows the pass.
- A request that is slow because of a slow provider holds an HTTP connection open; no
  provider timeout is imposed by the saga itself.
- The per-currency `holds` and `clearing` accounts are serialization points, like `external`.
- The `payouts` guard trigger can be bypassed by a superuser or owner (as ADR 0006).
- Downgrade of migration 0004 refuses if holds/clearing accounts exist and drops `payouts`.
- Throughput and latency are unmeasured.
