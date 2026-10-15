import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer

from ledgercore.config import get_settings
from ledgercore.db.posting import LineInput, post_entry

ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.nodeid.split("::")[0]:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        old = os.environ.get("LEDGER_DATABASE_URL")
        os.environ["LEDGER_DATABASE_URL"] = url
        get_settings.cache_clear()
        cfg = Config(str(ROOT / "alembic.ini"))
        command.upgrade(cfg, "head")  # sync fixture: env.py runs its own event loop
        yield url
        if old is None:
            os.environ.pop("LEDGER_DATABASE_URL", None)
        else:
            os.environ["LEDGER_DATABASE_URL"] = old
        get_settings.cache_clear()


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url, pool_size=25, max_overflow=0)
    # Append-only triggers block TRUNCATE; the test container user is a superuser, so
    # bypass triggers for cleanup only.
    async with eng.begin() as conn:
        await _wipe(conn)
    yield eng
    async with eng.begin() as conn:
        await _wipe(conn)
    await eng.dispose()


async def _wipe(conn) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(text("SET LOCAL session_replication_role = replica"))
    await conn.execute(
        text(
            "TRUNCATE payouts, outbox_events, idempotency_keys, balances, entry_lines, "
            "journal_entries, accounts RESTART IDENTITY CASCADE"
        )
    )


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def make_account(
    sm: async_sessionmaker[AsyncSession],
    currency: str = "USD",
    allow_negative: bool = False,
    funded: int = 0,
    source: int | None = None,
) -> int:
    async with sm() as s, s.begin():
        acct_id = (
            await s.execute(
                text(
                    "INSERT INTO accounts (currency, allow_negative) VALUES (:c, :n) RETURNING id"
                ),
                {"c": currency, "n": allow_negative},
            )
        ).scalar_one()
    if funded:
        if source is None:
            source = await make_account(sm, currency, allow_negative=True)
        async with sm() as s, s.begin():
            await post_entry(
                s,
                "fund",
                [LineInput(source, -funded, currency), LineInput(acct_id, funded, currency)],
            )
    return int(acct_id)
