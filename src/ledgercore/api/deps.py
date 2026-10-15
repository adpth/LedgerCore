"""Request-scoped dependencies: DB session/transaction and the Idempotency-Key header."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgercore.api.problems import ApiProblem

MAX_KEY_LENGTH = 255


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session and ONE transaction per request.

    ``session.begin()`` commits when the endpoint returns normally and rolls back if it
    raises (exceptions from the endpoint are re-raised into this generator). The
    dependency is declared with ``scope="function"`` so this exit code runs *before* the
    response is sent: a client never sees a 2xx for a transaction that then failed to
    commit (a commit failure becomes a 500 instead).
    """
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sm() as session, session.begin():
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session, scope="function")]


def _idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None:
        raise ApiProblem(400, "missing_idempotency_key", "The Idempotency-Key header is required.")
    if not (1 <= len(idempotency_key) <= MAX_KEY_LENGTH) or not idempotency_key.isprintable():
        raise ApiProblem(
            400,
            "invalid_idempotency_key",
            f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} printable characters.",
        )
    return idempotency_key


IdempotencyKeyDep = Annotated[str, Depends(_idempotency_key)]
