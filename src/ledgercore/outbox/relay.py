"""``ledgercore-outbox-relay``: publish ``outbox_events`` rows to Kafka, at least once.

Design (see docs/adr/0007-transactional-outbox.md):

* Single active relay. Whoever holds the session-level Postgres advisory lock
  ``LEADER_LOCK_KEY`` publishes; any other relay idles and retries each poll. A crashed
  leader's connection drops, which releases the lock and lets a standby take over. This
  (rather than ``FOR UPDATE SKIP LOCKED``) is what preserves per-aggregate ordering: with
  SKIP LOCKED two relays take disjoint batches and publish them concurrently, so a later
  event of an aggregate can overtake an earlier one.
* Rows are read in ``seq`` order and sent one at a time, awaiting the broker ack
  (idempotent producer, ``acks=all``), keyed by ``aggregate_id`` so Kafka keeps them in
  order per partition. Only then is ``published_at`` set.
* A crash or DB error between ack and mark re-publishes the row later with the same
  ``event_id``; consumers must dedupe on it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from aiokafka import AIOKafkaProducer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from ledgercore.config import Settings, get_settings
from ledgercore.db.session import create_engine

log = logging.getLogger("ledgercore.outbox.relay")

LEADER_LOCK_KEY = 0x6C65646765724F42  # arbitrary constant, fits in BIGINT

_FETCH = text(
    """
    SELECT seq, event_id, aggregate_id, event_type, payload
    FROM outbox_events WHERE published_at IS NULL ORDER BY seq LIMIT :n
    """
)
_MARK = text(
    "UPDATE outbox_events SET published_at = now() WHERE seq = ANY(:seqs) AND published_at IS NULL"
)
_LAG = text(
    """
    SELECT count(*) AS n, EXTRACT(EPOCH FROM now() - min(created_at)) AS age
    FROM outbox_events WHERE published_at IS NULL
    """
)


@dataclass(frozen=True, slots=True)
class Lag:
    unpublished: int
    oldest_age_s: float | None  # None when nothing is waiting


async def lag(session: AsyncSession) -> Lag:
    """Count and age (seconds) of the oldest unpublished event."""
    row = (await session.execute(_LAG)).one()
    return Lag(int(row.n), None if row.age is None else float(row.age))


class OutboxRelay:
    def __init__(
        self,
        engine: AsyncEngine,
        producer: AIOKafkaProducer,
        topic: str,
        batch_size: int = 100,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._engine = engine
        self._producer = producer
        self._topic = topic
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._lock_conn: AsyncConnection | None = None

    async def _is_leader(self) -> bool:
        """Hold (or try to take) the advisory lock on a dedicated autocommit connection."""
        if self._lock_conn is not None:
            try:
                await self._lock_conn.execute(text("SELECT 1"))
                return True
            except Exception:
                log.warning("leader connection lost; leadership dropped")
                await self._drop_lock_conn()
        conn = await self._engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK_KEY})
            ).scalar_one()
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._lock_conn = conn
        log.info("became relay leader")
        return True

    async def _drop_lock_conn(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None:
            try:
                await conn.invalidate()
            except Exception:
                log.exception("closing leader connection failed")

    async def _mark_published(self, session: AsyncSession, seqs: Sequence[int]) -> None:
        await session.execute(_MARK, {"seqs": list(seqs)})

    async def run_once(self) -> int:
        """Publish up to one batch. Returns rows published (0 if idle or not the leader)."""
        if not await self._is_leader():
            return 0
        sent: list[int] = []
        failure: Exception | None = None
        async with AsyncSession(self._engine) as session, session.begin():
            rows = (await session.execute(_FETCH, {"n": self._batch_size})).all()
            for row in rows:
                try:
                    await self._producer.send_and_wait(
                        self._topic,
                        value=json.dumps(row.payload).encode(),
                        key=row.aggregate_id.encode(),
                        headers=[
                            ("event_id", str(row.event_id).encode()),
                            ("event_type", row.event_type.encode()),
                        ],
                    )
                except Exception as exc:
                    # Stop at the first failure so later events never overtake this one.
                    failure = exc
                    break
                sent.append(row.seq)
            if sent:
                await self._mark_published(session, sent)
        if failure is not None:
            raise failure
        return len(sent)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    n = await self.run_once()
                except Exception:
                    log.exception("relay iteration failed; will retry")
                    n = 0
                if n >= self._batch_size:
                    continue  # more work is probably waiting
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._poll_interval_s)
        finally:
            await self.close()

    async def close(self) -> None:
        await self._drop_lock_conn()  # really closes the session: advisory lock released


def _make_producer(settings: Settings) -> AIOKafkaProducer:
    return AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        enable_idempotence=True,
        acks="all",
    )


async def _serve(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = create_engine(settings)
    producer = _make_producer(settings)
    await producer.start()
    try:
        relay = OutboxRelay(
            engine,
            producer,
            settings.outbox_topic,
            settings.outbox_batch_size,
            settings.outbox_poll_interval_s,
        )
        log.info("relay started topic=%s", settings.outbox_topic)
        await relay.run(stop)
    finally:
        await producer.stop()
        await engine.dispose()
        log.info("relay stopped")


async def _print_lag(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session:
            result = await lag(session)
        print(json.dumps({"unpublished": result.unpublished, "oldest_age_s": result.oldest_age_s}))
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledgercore-outbox-relay", description=__doc__)
    parser.add_argument("--lag", action="store_true", help="print outbox lag as JSON and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    try:
        asyncio.run(_print_lag(settings) if args.lag else _serve(settings))
    except Exception:
        log.exception("relay failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
