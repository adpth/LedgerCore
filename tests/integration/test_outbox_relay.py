"""Relay tests against real Postgres and a real Apache Kafka (KRaft) container."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from ledgercore.db.posting import LineInput, post_entry
from ledgercore.outbox.consumer import InMemoryProcessedStore, handle_once
from ledgercore.outbox.relay import OutboxRelay, lag
from ledgercore.outbox.writer import write_outbox_event
from tests.integration.conftest import make_account

SM = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    c = (
        DockerContainer("apache/kafka:3.9.0")
        .with_bind_ports(9092, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", "PLAINTEXT://:9092,CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"PLAINTEXT://127.0.0.1:{port}")
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env(
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT"
        )
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
    )
    c.waiting_for(LogMessageWaitStrategy("Kafka Server started").with_startup_timeout(90))
    with c:
        yield f"127.0.0.1:{port}"


@pytest.fixture
async def topic(kafka_bootstrap: str) -> str:
    name = f"test.{uuid.uuid4().hex[:12]}"
    admin = AIOKafkaAdminClient(bootstrap_servers=kafka_bootstrap)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(name, num_partitions=3, replication_factor=1)])
    finally:
        await admin.close()
    return name


async def new_producer(bootstrap: str) -> AIOKafkaProducer:
    p = AIOKafkaProducer(bootstrap_servers=bootstrap, enable_idempotence=True, acks="all")
    await p.start()
    return p


@pytest.fixture
async def producer(kafka_bootstrap: str) -> AsyncIterator[AIOKafkaProducer]:
    p = await new_producer(kafka_bootstrap)
    yield p
    await p.stop()


@dataclass
class Msg:
    key: str
    event_id: str
    event_type: str
    value: bytes
    payload: dict[str, Any]


async def read_topic(
    bootstrap: str, topic: str, expected: int, extra_wait_s: float = 1.5
) -> list[Msg]:
    """Read from the beginning until ``expected`` messages, then linger for strays."""
    import json

    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        group_id=None,
    )
    await consumer.start()
    out: list[Msg] = []
    try:
        deadline = time.monotonic() + 30
        linger_until: float | None = None
        while time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            for records in batch.values():
                for r in records:
                    h = {k: v.decode() for k, v in r.headers}
                    out.append(
                        Msg(
                            r.key.decode(),
                            h["event_id"],
                            h["event_type"],
                            r.value,
                            json.loads(r.value),
                        )
                    )
            if len(out) >= expected and linger_until is None:
                linger_until = time.monotonic() + extra_wait_s
            if linger_until is not None and time.monotonic() > linger_until:
                break
    finally:
        await consumer.stop()
    return out


async def drain(relay: OutboxRelay) -> int:
    total = 0
    while n := await relay.run_once():
        total += n
    return total


async def seed(sm: SM, per_aggregate: int, aggregates: list[str]) -> list[uuid.UUID]:
    """Interleaved writes, one transaction each, so seq order interleaves aggregates."""
    ids = []
    for n in range(per_aggregate):
        for agg in aggregates:
            async with sm() as s, s.begin():
                ids.append(await write_outbox_event(s, "agg", agg, "t", {"agg": agg, "n": n}))
    return ids


def assert_per_key_order(msgs: list[Msg]) -> None:
    by_key: dict[str, list[int]] = defaultdict(list)
    for m in msgs:
        by_key[m.key].append(m.payload["n"])
    for key, ns in by_key.items():
        assert ns == sorted(ns), f"{key} out of order: {ns}"


async def test_relay_publishes_exactly_the_events_in_order(
    engine: AsyncEngine,
    sessionmaker: SM,
    producer: AIOKafkaProducer,
    kafka_bootstrap: str,
    topic: str,
) -> None:
    ids = await seed(sessionmaker, 5, ["A", "B", "C"])
    a = await make_account(sessionmaker, allow_negative=True)
    b = await make_account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        posted = await post_entry(s, "t", [LineInput(a, -7, "USD"), LineInput(b, 7, "USD")])
    relay = OutboxRelay(engine, producer, topic, batch_size=4)
    try:
        # 15 seeded + 1 entry.posted, in batches of 4.
        assert await drain(relay) == 16
        msgs = await read_topic(kafka_bootstrap, topic, 16)
    finally:
        await relay.close()
    assert len(msgs) == 16
    assert {m.event_id for m in msgs} - {str(i) for i in ids} == {
        m.event_id for m in msgs if m.event_type == "entry.posted"
    }
    assert {str(i) for i in ids} <= {m.event_id for m in msgs}
    entry = next(m for m in msgs if m.event_type == "entry.posted")
    assert entry.key == str(posted.entry_id)
    assert entry.payload["entry_id"] == posted.entry_id
    assert_per_key_order([m for m in msgs if m.event_type == "t"])
    async with sessionmaker() as s:
        assert (await lag(s)).unpublished == 0


async def test_crash_between_publish_and_mark_republishes_and_consumer_dedupes(
    engine: AsyncEngine,
    sessionmaker: SM,
    producer: AIOKafkaProducer,
    kafka_bootstrap: str,
    topic: str,
) -> None:
    ids = await seed(sessionmaker, 2, ["A", "B"])

    class FlakyMark(OutboxRelay):
        failed = False

        async def _mark_published(self, session: AsyncSession, seqs: Any) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated crash before mark")
            await super()._mark_published(session, seqs)

    relay = FlakyMark(engine, producer, topic)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            await relay.run_once()
        async with sessionmaker() as s:
            assert (await lag(s)).unpublished == 4  # published but not marked
        assert await relay.run_once() == 4  # re-publish, now marked
        assert await relay.run_once() == 0
    finally:
        await relay.close()

    msgs = await read_topic(kafka_bootstrap, topic, 8)
    assert len(msgs) == 8
    assert sorted(m.event_id for m in msgs) == sorted(str(i) for i in ids * 2)  # same ids

    store = InMemoryProcessedStore()
    effects: list[bytes] = []

    async def handler(value: bytes) -> None:
        effects.append(value)

    for m in msgs:
        await handle_once(store, [("event_id", m.event_id.encode())], m.value, handler)
    assert len(effects) == 4


async def test_standby_relay_does_not_publish_while_leader_lives(
    engine: AsyncEngine,
    sessionmaker: SM,
    producer: AIOKafkaProducer,
    topic: str,
) -> None:
    await seed(sessionmaker, 1, ["A"])
    leader = OutboxRelay(engine, producer, topic)
    standby = OutboxRelay(engine, producer, topic)
    try:
        assert await leader.run_once() == 1
        await seed(sessionmaker, 1, ["A"])
        assert await standby.run_once() == 0  # leader holds the advisory lock
        await leader.close()  # leader "dies": lock released
        assert await standby.run_once() == 1
    finally:
        await leader.close()
        await standby.close()


async def test_two_concurrent_relays_lose_nothing_and_keep_order(
    engine: AsyncEngine,
    sessionmaker: SM,
    kafka_bootstrap: str,
    topic: str,
) -> None:
    aggregates = [f"agg-{i}" for i in range(6)]
    per_agg = 15
    p1, p2 = await new_producer(kafka_bootstrap), await new_producer(kafka_bootstrap)
    r1 = OutboxRelay(engine, p1, topic, batch_size=7, poll_interval_s=0.02)
    r2 = OutboxRelay(engine, p2, topic, batch_size=7, poll_interval_s=0.02)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(r.run(stop)) for r in (r1, r2)]
    written: list[uuid.UUID] = []

    async def writer(agg: str) -> None:  # one writer per aggregate: its events are serialized
        for n in range(per_agg):
            async with sessionmaker() as s, s.begin():
                written.append(await write_outbox_event(s, "agg", agg, "t", {"agg": agg, "n": n}))

    try:
        await asyncio.gather(*(writer(a) for a in aggregates))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            async with sessionmaker() as s:
                if (await lag(s)).unpublished == 0:
                    break
            await asyncio.sleep(0.1)
        stop.set()
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await p1.stop()
            await p2.stop()

    total = len(aggregates) * per_agg
    msgs = await read_topic(kafka_bootstrap, topic, total)
    assert len(msgs) == total  # nothing lost, nothing duplicated
    assert {m.event_id for m in msgs} == {str(i) for i in written}
    assert_per_key_order(msgs)
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")
            )
        ).scalar_one() == 0
