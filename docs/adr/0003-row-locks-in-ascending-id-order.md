# ADR 0003: Row locks in ascending ID order instead of SERIALIZABLE

Status: Accepted

## Context
Concurrent postings that touch the same accounts must not lose updates, overdraw, or
deadlock (A->B racing B->A).

## Decision
`post_entry` runs at the default READ COMMITTED level and locks explicitly:
1. `SELECT ... FROM accounts WHERE id IN (...) ORDER BY id FOR UPDATE`, then
2. `SELECT ... FROM balances WHERE account_id IN (...) ORDER BY account_id FOR UPDATE`.

Every transaction acquires locks in the same global order (accounts by ascending id, then
balance rows), so no wait-for cycle can form; opposing transfers serialize. The
non-negative check reads the balance only after the lock is held, so nobody can change it
before commit. `rebuild_balances` locks drifted accounts in the same order.

SERIALIZABLE was rejected as the primary mechanism: it would turn contention into
serialization failures that need a retry loop, and it conflicts with the idempotency
protocol (ADR 0005), which requires READ COMMITTED.

## Consequences
- No deadlocks from posting, no retry loop required.
- Hot accounts serialize. The per-currency system (external) account is touched by every
  deposit and withdrawal in that currency, so it is a serialization choke point for them.
- There is no `lock_timeout` or `statement_timeout`; a transaction stuck holding locks
  blocks others indefinitely, bounded only by the pool timeout on the waiting side.
- Throughput under contention has not been measured yet; a k6 load test is planned.
