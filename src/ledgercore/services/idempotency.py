"""Exactly-once execution of a mutating request, keyed by a client ``Idempotency-Key``.

Protocol (everything happens inside the CALLER's single database transaction, the same
one that writes the journal entry; this function never commits or rolls back itself):

1. **Claim** ``INSERT INTO idempotency_keys (key, request_hash, status='PENDING')
   ON CONFLICT (key) DO NOTHING RETURNING key``.

   Under READ COMMITTED, if another transaction has inserted the same key but not yet
   committed, Postgres makes our INSERT *wait* on that transaction (unique-index
   speculative insertion). When it finishes:

   * it committed  -> our INSERT does nothing (we lost the claim);
   * it rolled back -> our INSERT succeeds (we won the claim and run ``work``).

2. **Won** -> run ``work()``; then ``UPDATE`` the row to COMPLETED with the response
   status/body and ``entry_id``. The caller commits claim + entry + completion together.
   Nothing is ever committed in PENDING state, so a crash anywhere before COMMIT rolls
   back the entry *and* the key row together: the key is simply unused and a retry runs
   the operation afresh.

3. **Lost** -> ``SELECT ... FOR SHARE`` the row. Because step 1 already waited for the
   inserting transaction to end, the row we see is committed (READ COMMITTED takes a new
   snapshot per statement). FOR SHARE is belt-and-braces: should anything ever update the
   row in a separate transaction (e.g. an operator tool), we wait for it to finish rather
   than read a half-written response; it does not block other replayers (shared lock).

   * request_hash differs  -> :class:`IdempotencyKeyReuse` (HTTP 422);
   * COMPLETED             -> return the stored status + body, ``work`` is NOT re-run;
   * PENDING               -> unreachable with this protocol (PENDING is never
     committed); handled defensively as :class:`IdempotencyKeyInProgress` (HTTP 409).

**Concurrent twins** (same key, in flight at the same time): we chose *blocking* over an
immediate 409. The loser waits on the winner's uncommitted row (step 1) and then replays
the winner's committed response, so a client that retries early still gets the original
answer. Cost: the loser holds a DB connection while it waits, bounded by the winner's
transaction length (and by ``lock_timeout``/``statement_timeout`` if configured).

**Failed work is not cached.** If ``work`` raises (business errors such as
``InsufficientFunds``/``UnknownAccount``, or anything unexpected), the exception propagates
and the caller rolls back the whole transaction, *including the claim*. The key stays
unused and the same request may be retried, e.g. after the account has been funded. Only
successful responses are stored. Consequence: two attempts with one key can see different
error outcomes over time, but at most one success is ever recorded.

Requires READ COMMITTED (Postgres' default). Under REPEATABLE READ / SERIALIZABLE a lost
claim would surface as a serialization failure instead of a replay.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledgercore.db.models import IdempotencyKey
from ledgercore.services.errors import IdempotencyKeyInProgress, IdempotencyKeyReuse

PENDING = "PENDING"
COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class WorkResult:
    """What ``work`` produced: the response to store and the journal entry it wrote."""

    status_code: int
    body: dict[str, Any]
    entry_id: int | None = None


@dataclass(frozen=True, slots=True)
class IdempotentResponse:
    status_code: int
    body: dict[str, Any]
    replayed: bool


async def _claim(session: AsyncSession, key: str, fingerprint: str) -> bool:
    stmt = (
        pg_insert(IdempotencyKey)
        .values(key=key, request_hash=fingerprint, status=PENDING)
        .on_conflict_do_nothing(index_elements=[IdempotencyKey.key])
        .returning(IdempotencyKey.key)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _complete(session: AsyncSession, key: str, result: WorkResult) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.status == PENDING)
        .values(
            status=COMPLETED,
            response_status=result.status_code,
            response_body=result.body,
            entry_id=result.entry_id,
            completed_at=func.now(),
        )
    )


async def run_idempotent(
    session: AsyncSession,
    key: str,
    fingerprint: str,
    work: Callable[[], Awaitable[WorkResult]],
) -> IdempotentResponse:
    """Run ``work`` at most once per ``key`` (see module docstring). Caller commits."""
    # A lost claim whose row then vanished (only possible if keys are purged concurrently)
    # is retried once; a second disappearance is a genuine anomaly.
    for _ in range(2):
        if await _claim(session, key, fingerprint):
            result = await work()
            await _complete(session, key, result)
            return IdempotentResponse(result.status_code, result.body, replayed=False)

        row = (
            await session.execute(
                select(
                    IdempotencyKey.request_hash,
                    IdempotencyKey.status,
                    IdempotencyKey.response_status,
                    IdempotencyKey.response_body,
                )
                .where(IdempotencyKey.key == key)
                .with_for_update(read=True)
            )
        ).one_or_none()
        if row is None:
            continue
        if row.request_hash != fingerprint:
            raise IdempotencyKeyReuse(key)
        if row.status != COMPLETED or row.response_status is None or row.response_body is None:
            raise IdempotencyKeyInProgress(key)
        return IdempotentResponse(row.response_status, row.response_body, replayed=True)
    raise IdempotencyKeyInProgress(key)
