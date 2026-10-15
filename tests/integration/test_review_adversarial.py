"""Adversarial-review tests (see review report).

Passing tests pin behaviour that was probed and found correct. Tests marked
``xfail(strict=True)`` demonstrate real defects in ``src``; they flip to XPASS (and so fail
the run) once the defect is fixed, at which point the marker must be removed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledgercore.config import Settings
from ledgercore.db.posting import LineInput, find_drift, post_entry, rebuild_balances
from ledgercore.main import create_app

from .conftest import make_account

SM = async_sessionmaker[AsyncSession]
BIGINT_MAX = 2**63 - 1


@pytest.fixture
async def client(database_url: str, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=database_url, db_pool_size=10, db_max_overflow=10))
    async with (
        app.router.lifespan_context(app),
        # raise_app_exceptions=False: observe the real 500 a client would get.
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as c,
    ):
        yield c


def key() -> str:
    return str(uuid.uuid4())


async def post(c: AsyncClient, path: str, body: Any, k: str, **kw: Any) -> Response:
    return await c.post(path, json=body, headers={"Idempotency-Key": k}, **kw)


async def open_acct(c: AsyncClient, cur: str = "USD") -> int:
    r = await post(c, "/accounts", {"currency": cur}, key())
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def dep(c: AsyncClient, acct: int, amt: int, cur: str = "USD", **extra: Any) -> Response:
    body = {"account_id": acct, "amount_minor": amt, "currency": cur, **extra}
    return await post(c, "/deposits", body, key())


async def bal(c: AsyncClient, acct: int) -> int:
    return int((await c.get(f"/accounts/{acct}/balance")).json()["balance_minor"])


async def total_of_all_balances(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(text("SELECT COALESCE(SUM(balance_minor),0) FROM balances"))
            ).scalar_one()
        )


# ---- confirmed defects (xfail strict) ------------------------------------------------


async def test_rebuild_balances_does_not_clobber_concurrent_posting(
    engine: AsyncEngine, sessionmaker: SM
) -> None:
    a = await make_account(sessionmaker, funded=100)
    b = await make_account(sessionmaker)
    async with engine.begin() as conn:  # corrupt the projection of ``a``
        await conn.execute(
            text("UPDATE balances SET balance_minor = 101 WHERE account_id = :i"), {"i": a}
        )

    # T_post: a legitimate transfer holding the row locks, not yet committed.
    poster = sessionmaker()
    await poster.begin()
    await post_entry(poster, "t", [LineInput(a, -10, "USD"), LineInput(b, 10, "USD")])

    async def rebuild() -> None:
        async with sessionmaker() as s, s.begin():
            await rebuild_balances(s)

    task = asyncio.create_task(rebuild())
    await asyncio.sleep(0.7)  # rebuild has read its drift snapshot and is blocked on the lock
    assert not task.done()
    await poster.commit()
    await poster.close()
    await asyncio.wait_for(task, timeout=10)

    async with sessionmaker() as s:
        assert await find_drift(s) == []


async def test_nul_in_description_is_client_error_not_500(client: AsyncClient) -> None:
    acct = await open_acct(client)
    r = await dep(client, acct, 10, description="a\u0000b")
    assert 400 <= r.status_code < 500, (r.status_code, r.text)


async def test_balance_overflow_is_client_error_not_500(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_acct(client)
    assert (await dep(client, acct, BIGINT_MAX)).status_code == 201
    r = await dep(client, acct, 1)
    assert 400 <= r.status_code < 500, (r.status_code, r.text)
    assert await bal(client, acct) == BIGINT_MAX


async def test_path_param_spelling_does_not_break_replay(client: AsyncClient) -> None:
    a = await open_acct(client)
    assert (await dep(client, a, 50)).status_code == 201
    eid = 1
    k = key()
    r1 = await post(client, f"/entries/{eid}/reverse", None, k)
    assert r1.status_code == 201, r1.text
    r2 = await post(client, f"/entries/{eid:03d}/reverse", None, k)
    assert r2.status_code == 201 and r2.json() == r1.json()


# ---- probed and found correct (regression pins) -----------------------------------------


async def test_same_key_other_endpoint_or_path_param_is_rejected(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    a = await open_acct(client)
    assert (await dep(client, a, 100)).status_code == 201  # entry 1
    assert (await dep(client, a, 100)).status_code == 201  # entry 2
    k = key()
    assert (await post(client, "/entries/1/reverse", None, k)).status_code == 201
    r = await post(client, "/entries/2/reverse", None, k)  # same key, different path param
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reuse"
    # same key on a different endpoint with an equivalent-looking body
    r = await post(client, "/deposits", {"account_id": a, "amount_minor": 1, "currency": "USD"}, k)
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reuse"
    assert await bal(client, a) == 100  # only entry 1 reversed, nothing else applied


async def test_same_key_across_accounts_is_rejected(client: AsyncClient) -> None:
    a, b = await open_acct(client), await open_acct(client)
    k = key()
    body = {"account_id": a, "amount_minor": 5, "currency": "USD"}
    assert (await post(client, "/deposits", body, k)).status_code == 201
    r = await post(client, "/deposits", {**body, "account_id": b}, k)
    assert r.json()["code"] == "idempotency_key_reuse"
    assert await bal(client, b) == 0


async def test_raw_body_whitespace_order_and_header_case_do_not_change_replay(
    client: AsyncClient,
) -> None:
    a = await open_acct(client)
    k = key()
    raw1 = f'{{"account_id":{a},"amount_minor":25,"currency":"USD"}}'
    raw2 = (
        f'  {{ "currency" : "USD",\n "amount_minor":25 , "account_id" : {a} , "description":"" }} '
    )
    h = {"content-type": "application/json"}
    r1 = await client.post("/deposits", content=raw1, headers={**h, "Idempotency-Key": k})
    r2 = await client.post("/deposits", content=raw2, headers={**h, "idempotency-KEY": k})
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["idempotent-replayed"] == "true" and r1.json() == r2.json()
    assert await bal(client, a) == 25


async def test_unicode_normalisation_forms_are_distinct_requests(client: AsyncClient) -> None:
    """NFC vs NFD descriptions are different bytes; documented: reuse is rejected, not merged."""
    a = await open_acct(client)
    k = key()
    r1 = await post(
        client,
        "/deposits",
        {"account_id": a, "amount_minor": 1, "currency": "USD", "description": "café"},
        k,
    )
    r2 = await post(
        client,
        "/deposits",
        {"account_id": a, "amount_minor": 1, "currency": "USD", "description": "café"},
        k,
    )
    assert r1.status_code == 201 and r2.status_code == 422
    assert await bal(client, a) == 1


@pytest.mark.parametrize("cur", ["USD\n", "usd", "US", "USDX", "ＵＳＤ", " USD"])
async def test_currency_edge_cases_rejected(client: AsyncClient, cur: str) -> None:
    r = await post(client, "/accounts", {"currency": cur}, key())
    assert r.status_code == 422, r.text
    a = await open_acct(client)
    r = await dep(client, a, 5, cur=cur)
    assert r.status_code == 422, r.text


async def test_failed_request_with_key_then_success_with_same_key(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    a = await open_acct(client)
    k = key()
    body = {"account_id": a, "amount_minor": 50, "currency": "USD"}
    r = await post(client, "/withdrawals", body, k)
    assert r.json()["code"] == "insufficient_funds"
    assert (await dep(client, a, 50)).status_code == 201
    r = await post(client, "/withdrawals", body, k)  # key was not burned by the failure
    assert r.status_code == 201 and r.headers["idempotent-replayed"] == "false"
    assert await bal(client, a) == 0
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM idempotency_keys WHERE status<>'COMPLETED'")
            )
        ).scalar_one()
    assert n == 0


async def test_reversal_of_reversal_and_global_conservation(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    a, b = await open_acct(client), await open_acct(client)
    assert (await dep(client, a, 100)).status_code == 201  # e1
    r = await post(
        client,
        "/transfers",
        {"from_account_id": a, "to_account_id": b, "amount_minor": 60, "currency": "USD"},
        key(),
    )
    t = r.json()["id"]
    rev = (await post(client, f"/entries/{t}/reverse", None, key())).json()
    assert (await bal(client, a), await bal(client, b)) == (100, 0)
    # reversing the reversal re-applies the transfer exactly once; original stays reversed
    again = await post(client, f"/entries/{rev['id']}/reverse", None, key())
    assert again.status_code == 201
    assert (await bal(client, a), await bal(client, b)) == (40, 60)
    r = await post(client, f"/entries/{t}/reverse", None, key())
    assert r.status_code == 409
    r = await post(client, f"/entries/{rev['id']}/reverse", None, key())
    assert r.status_code == 409
    assert await total_of_all_balances(engine) == 0  # money neither created nor destroyed


async def test_reversing_deposit_after_spend_is_rejected_and_atomic(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    a, b = await open_acct(client), await open_acct(client)
    d = (await dep(client, a, 100)).json()["id"]
    r = await post(
        client,
        "/transfers",
        {"from_account_id": a, "to_account_id": b, "amount_minor": 70, "currency": "USD"},
        key(),
    )
    assert r.status_code == 201
    r = await post(client, f"/entries/{d}/reverse", None, key())
    assert r.status_code == 422 and r.json()["code"] == "insufficient_funds"
    assert (await bal(client, a), await bal(client, b)) == (30, 70)
    assert await total_of_all_balances(engine) == 0


async def test_system_account_cannot_be_driven_through_api(client: AsyncClient) -> None:
    a = await open_acct(client)
    assert (await dep(client, a, 10)).status_code == 201
    async_sys = None
    r = await client.get(f"/accounts/{a}/entries")
    for ln in r.json()["entries"][0]["lines"]:
        if ln["account_id"] != a:
            async_sys = ln["account_id"]
    assert async_sys is not None
    for path, body in [
        ("/deposits", {"account_id": async_sys, "amount_minor": 5, "currency": "USD"}),
        ("/withdrawals", {"account_id": async_sys, "amount_minor": 5, "currency": "USD"}),
        (
            "/transfers",
            {
                "from_account_id": async_sys,
                "to_account_id": a,
                "amount_minor": 5,
                "currency": "USD",
            },
        ),
        (
            "/transfers",
            {
                "from_account_id": a,
                "to_account_id": async_sys,
                "amount_minor": 5,
                "currency": "USD",
            },
        ),
    ]:
        r = await post(client, path, body, key())
        assert r.status_code == 422 and r.json()["code"] == "system_account_not_allowed", (
            path,
            r.text,
        )


async def test_concurrent_first_deposits_in_new_currencies_no_deadlock(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Lazy system-account creation racing with transfers/withdrawals/reversals."""
    curs = ["USD", "EUR", "GBP"]
    accts = {c: [await open_acct(client, c) for _ in range(3)] for c in curs}
    jobs = []
    for i in range(30):
        c = curs[i % 3]
        a1, a2, _ = accts[c]
        jobs.append(dep(client, a1, 100, cur=c))
        jobs.append(dep(client, a2, 100, cur=c))
    rs = await asyncio.wait_for(asyncio.gather(*jobs), timeout=60)
    assert all(r.status_code == 201 for r in rs), [r.text for r in rs if r.status_code != 201]
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM accounts WHERE is_system"))).scalar_one()
    assert n == 3
    assert await total_of_all_balances(engine) == 0
    json.dumps({})  # keep json import used for debugging helpers
