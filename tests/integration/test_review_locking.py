"""Mutation-driven tests: they fail if the ORDER BY / FOR UPDATE in ``post_entry`` or the
per-line double-entry trigger are removed (the pre-existing suite passed with each of them
removed; see review report)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ledgercore.db.posting import LineInput, post_entry

from .conftest import make_account

SM = async_sessionmaker[AsyncSession]
# NO KEY UPDATE: conflicts with post_entry's FOR UPDATE but NOT with the incidental
# FOR KEY SHARE locks that FK checks take, so it detects the explicit lock only.
NOWAIT = "SELECT id FROM accounts WHERE id = :i FOR NO KEY UPDATE NOWAIT"


async def _is_row_locked(engine: AsyncEngine, account_id: int) -> bool:
    try:
        async with engine.begin() as conn:
            await conn.execute(text(NOWAIT), {"i": account_id})
        return False
    except DBAPIError as exc:
        assert "could not obtain lock" in str(exc)
        return True


async def test_post_entry_holds_account_row_locks_until_commit(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Kills: removing FOR UPDATE on the accounts SELECT (accounts rows are never UPDATEd,
    so without the explicit lock nothing else would serialise on them)."""
    a = await make_account(sessionmaker, funded=100)
    b = await make_account(sessionmaker)
    s = sessionmaker()
    await s.begin()
    try:
        await post_entry(s, "t", [LineInput(a, -5, "USD"), LineInput(b, 5, "USD")])
        assert await _is_row_locked(engine, a)
        assert await _is_row_locked(engine, b)
    finally:  # never leave locks behind: fixture teardown TRUNCATEs
        await s.rollback()
        await s.close()
    assert not await _is_row_locked(engine, a)


async def test_accounts_are_locked_in_ascending_id_order_even_if_planner_scans_otherwise(
    database_url: str, engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Kills: removing ORDER BY id. Force a seq scan whose physical order is (b, a) so an
    un-ordered FOR UPDATE would lock b first; then block b and check that the lower id ``a``
    was already locked by the waiting poster."""
    a = await make_account(sessionmaker)
    b = await make_account(sessionmaker, funded=100)
    assert a < b
    async with engine.begin() as conn:  # rewrite tuple of ``a`` -> physically after ``b``
        await conn.execute(text("UPDATE accounts SET created_at = now() WHERE id = :i"), {"i": a})

    seqscan = create_async_engine(
        database_url,
        connect_args={
            "server_settings": {
                "enable_indexscan": "off",
                "enable_bitmapscan": "off",
                "enable_indexonlyscan": "off",
            }
        },
    )
    blocker = sessionmaker()
    await blocker.begin()
    await blocker.execute(text("SELECT id FROM accounts WHERE id = :i FOR UPDATE"), {"i": b})

    async def poster() -> None:
        async with async_sessionmaker(seqscan)() as s, s.begin():
            await post_entry(s, "t", [LineInput(b, -5, "USD"), LineInput(a, 5, "USD")])

    task = asyncio.create_task(poster())
    try:
        await asyncio.sleep(0.7)
        assert not task.done()  # waiting on b
        assert await _is_row_locked(engine, a), "lower id must be locked first"
    finally:
        await blocker.rollback()
        await blocker.close()
        await asyncio.wait_for(task, timeout=15)
        await seqscan.dispose()


async def test_trigger_rejects_unbalanced_line_added_to_committed_entry(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    """Kills: dropping the entry_lines constraint trigger (the journal_entries trigger only
    fires for the header insert, so it cannot see lines added in a LATER transaction)."""
    a = await make_account(sessionmaker)
    b = await make_account(sessionmaker)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = DEFAULT"))
        eid = (
            await conn.execute(
                text("INSERT INTO journal_entries (entry_type) VALUES ('x') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                "VALUES (:e, :a, 10, 'USD'), (:e, :b, -10, 'USD')"
            ),
            {"e": eid, "a": a, "b": b},
        )
    with pytest.raises(DBAPIError, match="unbalanced"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                    "VALUES (:e, :a, 7, 'USD')"
                ),
                {"e": eid, "a": a},
            )
