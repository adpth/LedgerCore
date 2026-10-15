import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledgercore.db.errors import (
    CurrencyMismatch,
    EntryAlreadyReversed,
    InsufficientFunds,
    UnbalancedEntry,
    UnknownAccount,
)
from ledgercore.db.posting import LineInput, find_drift, post_entry, rebuild_balances

from .conftest import make_account

SM = async_sessionmaker[AsyncSession]


async def balance(sm: SM, account_id: int) -> int:
    async with sm() as s:
        return int(
            (
                await s.execute(
                    text("SELECT balance_minor FROM balances WHERE account_id = :i"),
                    {"i": account_id},
                )
            ).scalar_one()
        )


async def transfer(sm: SM, src: int, dst: int, amount: int, cur: str = "USD") -> None:
    async with sm() as s, s.begin():
        await post_entry(s, "transfer", [LineInput(src, -amount, cur), LineInput(dst, amount, cur)])


# ---- raw SQL: DB enforces invariants without Python -------------------------------


async def test_trigger_rejects_unbalanced_entry_via_raw_sql(engine: AsyncEngine, sessionmaker: SM):
    a = await make_account(sessionmaker)
    b = await make_account(sessionmaker)
    with pytest.raises(DBAPIError, match="unbalanced"):
        async with engine.begin() as conn:
            eid = (
                await conn.execute(
                    text("INSERT INTO journal_entries (entry_type) VALUES ('x') RETURNING id")
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                    "VALUES (:e, :a, 100, 'USD'), (:e, :b, -99, 'USD')"
                ),
                {"e": eid, "a": a, "b": b},
            )
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM journal_entries"))).scalar_one()
    assert n == 0


async def test_trigger_accepts_balanced_entry_via_raw_sql(engine: AsyncEngine, sessionmaker: SM):
    a = await make_account(sessionmaker)
    b = await make_account(sessionmaker)
    async with engine.begin() as conn:
        eid = (
            await conn.execute(
                text("INSERT INTO journal_entries (entry_type) VALUES ('x') RETURNING id")
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                "VALUES (:e, :a, 100, 'USD'), (:e, :b, -100, 'USD')"
            ),
            {"e": eid, "a": a, "b": b},
        )


async def test_single_line_entry_rejected(engine: AsyncEngine, sessionmaker: SM):
    a = await make_account(sessionmaker)
    with pytest.raises(DBAPIError, match="at least 2 lines"):
        async with engine.begin() as conn:
            eid = (
                await conn.execute(
                    text("INSERT INTO journal_entries (entry_type) VALUES ('x') RETURNING id")
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                    "VALUES (:e, :a, 100, 'USD')"
                ),
                {"e": eid, "a": a},
            )


async def test_entry_with_no_lines_rejected(engine: AsyncEngine):
    with pytest.raises(DBAPIError, match="at least 2 lines"):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO journal_entries (entry_type) VALUES ('x')"))


async def test_per_currency_balance_enforced(engine: AsyncEngine, sessionmaker: SM):
    usd = await make_account(sessionmaker, "USD")
    eur = await make_account(sessionmaker, "EUR")
    with pytest.raises(DBAPIError, match="unbalanced"):
        async with engine.begin() as conn:
            eid = (
                await conn.execute(
                    text("INSERT INTO journal_entries (entry_type) VALUES ('x') RETURNING id")
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                    "VALUES (:e, :a, 100, 'USD'), (:e, :b, -100, 'EUR')"
                ),
                {"e": eid, "a": usd, "b": eur},
            )


@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
@pytest.mark.parametrize("table", ["entry_lines", "journal_entries"])
async def test_append_only_rejects_update_delete(
    engine: AsyncEngine, sessionmaker: SM, table: str, op: str
):
    a = await make_account(sessionmaker, funded=100)
    assert a
    sql = f"UPDATE {table} SET created_at = now()" if op == "UPDATE" else f"DELETE FROM {table}"
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text(sql))


@pytest.mark.parametrize("table", ["entry_lines", "journal_entries"])
async def test_append_only_rejects_truncate(engine: AsyncEngine, table: str):
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {table} CASCADE"))


async def test_db_rejects_currency_mismatch_via_raw_sql(engine: AsyncEngine, sessionmaker: SM):
    a = await make_account(sessionmaker, "USD")
    with pytest.raises(DBAPIError, match="fk_entry_lines_account_currency"):
        async with engine.begin() as conn:
            eid = (
                await conn.execute(
                    text("INSERT INTO journal_entries (entry_type) VALUES ('x') RETURNING id")
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO entry_lines (entry_id, account_id, amount_minor, currency) "
                    "VALUES (:e, :a, 5, 'EUR')"
                ),
                {"e": eid, "a": a},
            )


async def test_db_rejects_negative_balance_unless_allowed(engine: AsyncEngine, sessionmaker: SM):
    strict = await make_account(sessionmaker)
    loose = await make_account(sessionmaker, allow_negative=True)
    with pytest.raises(DBAPIError, match="would be negative"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE balances SET balance_minor = -1 WHERE account_id = :i"), {"i": strict}
            )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE balances SET balance_minor = -1 WHERE account_id = :i"), {"i": loose}
        )


# ---- post_entry behaviour ------------------------------------------------------------


async def test_post_entry_errors(sessionmaker: SM):
    a = await make_account(sessionmaker, funded=50)
    b = await make_account(sessionmaker)
    eur = await make_account(sessionmaker, "EUR")
    async with sessionmaker() as s:
        with pytest.raises(UnbalancedEntry):
            await post_entry(s, "t", [LineInput(a, -10, "USD"), LineInput(b, 9, "USD")])
        with pytest.raises(UnbalancedEntry):
            await post_entry(s, "t", [LineInput(a, -10, "USD")])
        with pytest.raises(UnknownAccount):
            await post_entry(s, "t", [LineInput(a, -10, "USD"), LineInput(9999, 10, "USD")])
        with pytest.raises(CurrencyMismatch):
            await post_entry(s, "t", [LineInput(a, -10, "USD"), LineInput(eur, 10, "USD")])
        with pytest.raises(InsufficientFunds):
            await post_entry(s, "t", [LineInput(a, -51, "USD"), LineInput(b, 51, "USD")])
    assert await balance(sessionmaker, a) == 50


async def test_double_reversal_raises(sessionmaker: SM):
    a = await make_account(sessionmaker, funded=100)
    b = await make_account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        orig = await post_entry(s, "t", [LineInput(a, -40, "USD"), LineInput(b, 40, "USD")])
    rev_lines = [LineInput(a, 40, "USD"), LineInput(b, -40, "USD")]
    async with sessionmaker() as s, s.begin():
        await post_entry(s, "reversal", rev_lines, reverses_entry_id=orig.entry_id)
    async with sessionmaker() as s:
        with pytest.raises(EntryAlreadyReversed):
            await post_entry(s, "reversal", rev_lines, reverses_entry_id=orig.entry_id)
    assert await balance(sessionmaker, a) == 100
    assert await balance(sessionmaker, b) == 0


async def test_concurrent_double_reversal_only_one_wins(sessionmaker: SM):
    a = await make_account(sessionmaker, funded=100)
    b = await make_account(sessionmaker)
    async with sessionmaker() as s, s.begin():
        orig = await post_entry(s, "t", [LineInput(a, -40, "USD"), LineInput(b, 40, "USD")])

    async def rev() -> bool:
        try:
            async with sessionmaker() as s, s.begin():
                await post_entry(
                    s,
                    "reversal",
                    [LineInput(a, 40, "USD"), LineInput(b, -40, "USD")],
                    reverses_entry_id=orig.entry_id,
                )
            return True
        except EntryAlreadyReversed:
            return False

    results = await asyncio.gather(*[rev() for _ in range(5)])
    assert results.count(True) == 1
    assert await balance(sessionmaker, a) == 100


# ---- concurrency ---------------------------------------------------------------------


async def test_concurrent_drain_never_overdraws(sessionmaker: SM):
    src = await make_account(sessionmaker, funded=1000)
    dst = await make_account(sessionmaker)

    async def attempt() -> bool:
        try:
            await transfer(sessionmaker, src, dst, 30)
            return True
        except InsufficientFunds:
            return False

    results = await asyncio.gather(*[attempt() for _ in range(50)])
    assert results.count(True) == 1000 // 30  # 33
    assert await balance(sessionmaker, src) == 1000 - 33 * 30
    assert await balance(sessionmaker, dst) == 33 * 30
    async with sessionmaker() as s:
        assert await find_drift(s) == []


async def test_opposite_transfers_do_not_deadlock(sessionmaker: SM):
    a = await make_account(sessionmaker, funded=100_000)
    b = await make_account(sessionmaker, funded=100_000)
    jobs = [
        transfer(sessionmaker, a, b, 7) if i % 2 else transfer(sessionmaker, b, a, 11)
        for i in range(60)
    ]
    await asyncio.wait_for(asyncio.gather(*jobs), timeout=60)  # DeadlockDetected would raise
    assert await balance(sessionmaker, a) == 100_000 - 30 * 7 + 30 * 11
    assert await balance(sessionmaker, b) == 100_000 + 30 * 7 - 30 * 11


# ---- projection drift -----------------------------------------------------------------


async def test_find_drift_clean_then_detects_corruption_and_rebuilds(
    engine: AsyncEngine, sessionmaker: SM
):
    a = await make_account(sessionmaker, funded=500)
    b = await make_account(sessionmaker)
    await asyncio.gather(*[transfer(sessionmaker, a, b, 10) for _ in range(20)])
    async with sessionmaker() as s:
        assert await find_drift(s) == []

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE balances SET balance_minor = balance_minor + 1 WHERE account_id = :i"),
            {"i": b},
        )
    async with sessionmaker() as s:
        drift = await find_drift(s)
    assert [(d.account_id, d.projected_minor, d.computed_minor) for d in drift] == [(b, 201, 200)]

    async with sessionmaker() as s, s.begin():
        fixed = await rebuild_balances(s)
    assert [d.account_id for d in fixed] == [b]
    async with sessionmaker() as s:
        assert await find_drift(s) == []
    assert await balance(sessionmaker, b) == 200
