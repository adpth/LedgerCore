"""Write side of the transactional outbox."""

import uuid
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledgercore.db.models import OutboxEvent


async def write_outbox_event(
    session: AsyncSession,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert an outbox row in the caller's transaction (never commits).

    ``payload`` must already be JSON-serializable. Events of the same aggregate are only
    guaranteed to be delivered in order if their writers are serialized (e.g. by a row lock
    on the aggregate), because ``seq`` is assigned at insert time, not at commit time.
    """
    event_id = event_id or uuid.uuid4()
    await session.execute(
        insert(OutboxEvent).values(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
    )
    return event_id
