"""Reference idempotent-consumer helper: dedupe on the ``event_id`` header.

The relay is at-least-once, so a consumer can see the same event twice. This is the
smallest thing that honours that contract. ``InMemoryProcessedStore`` is for tests and
demos only: a real consumer must persist the processed set in the SAME transaction as the
effect (a table with a unique ``event_id``), otherwise a crash between the two either
loses or repeats the effect.
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol


class ProcessedStore(Protocol):
    async def add_if_new(self, event_id: str) -> bool:
        """Record ``event_id``; return True only the first time it is seen."""
        ...


class InMemoryProcessedStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def add_if_new(self, event_id: str) -> bool:
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        return True


def event_id_of(headers: Iterable[tuple[str, bytes]]) -> str:
    for name, value in headers:
        if name == "event_id":
            return value.decode()
    raise ValueError("message has no event_id header")


async def handle_once(
    store: ProcessedStore,
    headers: Iterable[tuple[str, bytes]],
    value: bytes,
    handler: Callable[[bytes], Awaitable[None]],
) -> bool:
    """Run ``handler`` unless this event_id was already processed. True if it ran."""
    if not await store.add_if_new(event_id_of(headers)):
        return False
    await handler(value)
    return True
