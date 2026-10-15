# ADR 0005: Idempotency transaction protocol

Status: Accepted

## Context
Clients retry. A retried deposit or transfer must not execute twice, even when the
original and the retry run concurrently or the server crashes mid-request.

## Decision
Mutating POSTs require an `Idempotency-Key` header. `run_idempotent` runs entirely inside
the request's single transaction (the same one that writes the journal entry):
1. Claim: `INSERT ... ON CONFLICT (key) DO NOTHING RETURNING key` with status PENDING.
2. Won: run the work, mark the row COMPLETED with the response status/body and entry id.
   Claim, entry and completion commit atomically; PENDING is never committed.
3. Lost: `SELECT ... FOR SHARE` the row. A concurrent twin blocks on the winner's
   uncommitted key row (block-then-replay, not an immediate 409), then replays the
   committed response. A different request fingerprint under the same key is 422
   `idempotency_key_reuse`.
4. Failures are not cached: if the work raises, the whole transaction, including the
   claim, rolls back, so the key stays unused and the request can be retried.

Requires READ COMMITTED (the Postgres default). Under REPEATABLE READ or SERIALIZABLE a
lost claim would surface as a serialization failure rather than a replay.

## Consequences
- Exactly-once success per key; crash-safe because nothing is committed half-done.
- A waiting twin holds a pooled connection for up to the winner's transaction length;
  with no `lock_timeout`/`statement_timeout` that wait is unbounded in theory.
- Two attempts with one key can see different error outcomes over time; at most one
  success is ever recorded.
- Keys are never cleaned up, so `idempotency_keys` grows without bound and a key is
  effectively reserved forever once used. A retention job is not yet written.
