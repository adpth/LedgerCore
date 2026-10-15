"""Outbox write-side tests: same-transaction guarantee, append-only guard, lag. No Kafka."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledgercore.config import Settings
from ledgercore.db.errors import EntryAlreadyReversed
from ledgercore.db.posting import LineInput, post_entry
from ledgercore.main import create_app
from ledgercore.outbox.relay import lag
from ledgercore.outbox.writer import write_outbox_event
from tests.integration.conftest import make_account

SM = async_sessionmaker[AsyncSession]


async def rows(engine: AsyncEngine, where: str = "true") -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(text(f"SELECT * FROM outbox_events WHERE {where} ORDER BY seq"))
            ).all()
        )


async def test_entry_writes_outbox_row_in_same_transaction(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    a = await make_account(sessionmaker, allow_negative=True)
    b = await make_account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        posted = await post_entry(s, "transfer", [LineInput(a, -5, "USD"), LineInput(b, 5, "USD")])
        # Another connection must not see the event before commit.
        assert await rows(engine, f"aggregate_id = '{posted.entry_id}'") == []
    (row,) = await rows(engine, f"aggregate_id = '{posted.entry_id}'")
    assert row.event_type == "entry.posted"
    assert row.aggregate_type == "journal_entry"
    assert row.published_at is None
    assert isinstance(row.event_id, uuid.UUID)
    p = row.payload
    assert p["entry_id"] == posted.entry_id
    assert p["entry_type"] == "transfer"
    assert p["reverses_entry_id"] is None
    assert p["lines"] == [
        {"account_id": a, "amount_minor": -5, "currency": "USD"},
        {"account_id": b, "amount_minor": 5, "currency": "USD"},
    ]
    assert p["created_at"]


async def test_rollback_of_entry_leaves_no_outbox_row(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    a = await make_account(sessionmaker, allow_negative=True)
    b = await make_account(sessionmaker)
    before = len(await rows(engine))
    with pytest.raises(RuntimeError):
        async with sessionmaker() as s, s.begin():
            await post_entry(s, "transfer", [LineInput(a, -5, "USD"), LineInput(b, 5, "USD")])
            raise RuntimeError("abort after post_entry")
    assert len(await rows(engine)) == before


async def test_failed_post_leaves_no_outbox_row(engine: AsyncEngine, sessionmaker: SM) -> None:
    a = await make_account(sessionmaker, allow_negative=True)
    b = await make_account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        first = await post_entry(s, "t", [LineInput(a, -5, "USD"), LineInput(b, 5, "USD")])
        await post_entry(
            s,
            "r",
            [LineInput(a, 5, "USD"), LineInput(b, -5, "USD")],
            reverses_entry_id=first.entry_id,
        )
    before = len(await rows(engine))
    with pytest.raises(EntryAlreadyReversed):
        async with sessionmaker() as s, s.begin():
            await post_entry(
                s,
                "r2",
                [LineInput(a, 5, "USD"), LineInput(b, -5, "USD")],
                reverses_entry_id=first.entry_id,
            )
    assert len(await rows(engine)) == before


@pytest.fixture
async def client(database_url: str, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=database_url))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_reversal_and_idempotent_replay_one_row_per_real_entry(
    engine: AsyncEngine, client: AsyncClient
) -> None:
    async def post(path: str, body: dict[str, Any], key: str) -> Any:
        r = await client.post(path, json=body, headers={"Idempotency-Key": key})
        assert r.status_code == 201, r.text
        return r

    acct = (await post("/accounts", {"currency": "USD"}, "k-open")).json()["id"]
    dep = {"account_id": acct, "amount_minor": 100, "currency": "USD"}
    first = await post("/deposits", dep, "k-dep")
    assert len(await rows(engine)) == 1
    replay = await client.post("/deposits", json=dep, headers={"Idempotency-Key": "k-dep"})
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert len(await rows(engine)) == 1  # replay adds no event

    entry_id = first.json()["id"]
    rev = await post(f"/entries/{entry_id}/reverse", {}, "k-rev")
    again = await client.post(
        f"/entries/{entry_id}/reverse", json={}, headers={"Idempotency-Key": "k-rev"}
    )
    assert again.headers["Idempotent-Replayed"] == "true"
    dup = await client.post(
        f"/entries/{entry_id}/reverse", json={}, headers={"Idempotency-Key": "k-rev-2"}
    )
    assert dup.status_code == 409
    got = await rows(engine)
    assert [r.payload["entry_id"] for r in got] == [entry_id, rev.json()["id"]]
    assert got[1].payload["reverses_entry_id"] == entry_id


async def test_only_published_at_may_change(engine: AsyncEngine, sessionmaker: SM) -> None:
    async with sessionmaker() as s, s.begin():
        await write_outbox_event(s, "agg", "1", "t", {"n": 1})

    async def attempt(sql: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(text(sql))

    for sql in (
        "UPDATE outbox_events SET payload = '{}'::jsonb",
        "UPDATE outbox_events SET event_type = 'x'",
        "UPDATE outbox_events SET seq = seq",  # identity ALWAYS rejects before the trigger
        "DELETE FROM outbox_events",
        "TRUNCATE outbox_events",
    ):
        with pytest.raises(DBAPIError):
            await attempt(sql)
    await attempt("UPDATE outbox_events SET published_at = now()")
    assert (await rows(engine))[0].published_at is not None


async def test_lag_reports_count_and_oldest_age(engine: AsyncEngine, sessionmaker: SM) -> None:
    async with sessionmaker() as s:
        empty = await lag(s)
    assert (empty.unpublished, empty.oldest_age_s) == (0, None)

    async with sessionmaker() as s, s.begin():
        for n in range(3):
            await write_outbox_event(s, "agg", "1", "t", {"n": n})
    async with engine.begin() as conn:
        # Backdating needs the guard bypassed (test container user is a superuser).
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(
            text("UPDATE outbox_events SET created_at = now() - interval '1 hour' WHERE seq = 1")
        )
    async with sessionmaker() as s:
        got = await lag(s)
    assert got.unpublished == 3
    assert got.oldest_age_s is not None and 3590 < got.oldest_age_s < 3700

    async with engine.begin() as conn:
        await conn.execute(text("UPDATE outbox_events SET published_at = now() WHERE seq <= 2"))
    async with sessionmaker() as s:
        got = await lag(s)
    assert got.unpublished == 1
    assert got.oldest_age_s is not None and got.oldest_age_s < 60
