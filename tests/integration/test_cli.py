import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledgercore import cli

from .conftest import make_account

SM = async_sessionmaker[AsyncSession]


async def test_clean_db_exits_zero(
    engine: AsyncEngine, sessionmaker: SM, capsys: pytest.CaptureFixture[str]
):
    await make_account(sessionmaker, funded=100)
    assert await cli.run([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["drift_count"] == 0
    assert report["accounts"] == []


async def test_drift_exits_one_with_json_and_repair_fixes(
    engine: AsyncEngine, sessionmaker: SM, capsys: pytest.CaptureFixture[str]
):
    acct = await make_account(sessionmaker, funded=100)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE balances SET balance_minor = balance_minor + 5 WHERE account_id = :i"),
            {"i": acct},
        )

    assert await cli.run([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["drift_count"] == 1
    assert report["accounts"] == [
        {"account_id": acct, "projected_minor": 105, "computed_minor": 100}
    ]

    # --repair fixes the projection but still reports that drift was found.
    assert await cli.run(["--repair"]) == 1
    assert json.loads(capsys.readouterr().out)["repaired"] is True
    assert await cli.run([]) == 0
    assert json.loads(capsys.readouterr().out)["drift_count"] == 0
