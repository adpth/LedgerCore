"""HTTP API + idempotency tests against a real Postgres (Testcontainers)."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledgercore.config import Settings
from ledgercore.domain import Money
from ledgercore.main import create_app
from ledgercore.services import idempotency as idem
from ledgercore.services import ledger
from ledgercore.services.idempotency import WorkResult, run_idempotent

SM = async_sessionmaker[AsyncSession]


def _app(database_url: str) -> Any:
    return create_app(Settings(database_url=database_url, db_pool_size=20, db_max_overflow=40))


@pytest.fixture
async def client(database_url: str, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    # ``engine`` wipes the DB before/after. ASGITransport does not run lifespan events, so
    # drive the lifespan explicitly (creates/disposes the app's own engine).
    app = _app(database_url)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


def key() -> str:
    return str(uuid.uuid4())


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def n_entries(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "SELECT count(*) FROM journal_entries"))


async def post(c: AsyncClient, path: str, body: dict[str, Any] | None, k: str | None) -> Response:
    headers = {"Idempotency-Key": k} if k is not None else {}
    return await c.post(path, json=body, headers=headers)


async def open_account(c: AsyncClient, currency: str = "USD") -> int:
    r = await post(c, "/accounts", {"currency": currency}, key())
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def deposit(c: AsyncClient, acct: int, amount: int, k: str | None = None) -> Response:
    body = {"account_id": acct, "amount_minor": amount, "currency": "USD"}
    return await post(c, "/deposits", body, k or key())


async def balance(c: AsyncClient, acct: int) -> int:
    r = await c.get(f"/accounts/{acct}/balance")
    assert r.status_code == 200, r.text
    return int(r.json()["balance_minor"])


def assert_problem(r: Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = r.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == f"urn:ledgercore:problem:{code}"
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str)
    assert body["instance"] == r.request.url.path
    return body


# ---- idempotency --------------------------------------------------------------------


async def test_deposit_replay_returns_identical_response_and_one_entry(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    k = key()
    first = await deposit(client, acct, 500, k)
    second = await deposit(client, acct, 500, k)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.headers["Idempotent-Replayed"] == "true"
    assert await n_entries(engine) == 1
    assert await balance(client, acct) == 500
    row = await scalar(
        engine, "SELECT entry_id FROM idempotency_keys WHERE key = :k AND status = 'COMPLETED'", k=k
    )
    assert row == first.json()["id"]


async def test_key_order_in_body_does_not_change_fingerprint(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    k = key()
    a = await post(
        client, "/deposits", {"account_id": acct, "amount_minor": 7, "currency": "USD"}, k
    )
    b = await post(
        client, "/deposits", {"currency": "USD", "amount_minor": 7, "account_id": acct}, k
    )
    assert a.json() == b.json()
    assert await n_entries(engine) == 1


async def test_same_key_different_body_is_422(client: AsyncClient, engine: AsyncEngine) -> None:
    acct = await open_account(client)
    k = key()
    assert (await deposit(client, acct, 500, k)).status_code == 201
    assert_problem(await deposit(client, acct, 501, k), 422, "idempotency_key_reuse")
    # Same key on a different endpoint is also a different request.
    body = {"account_id": acct, "amount_minor": 500, "currency": "USD"}
    assert_problem(await post(client, "/withdrawals", body, k), 422, "idempotency_key_reuse")
    assert await n_entries(engine) == 1
    assert await balance(client, acct) == 500


async def test_100_concurrent_identical_requests_one_entry(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    k = key()
    responses = await asyncio.gather(*(deposit(client, acct, 250, k) for _ in range(100)))
    assert {r.status_code for r in responses} == {201}
    bodies = [r.json() for r in responses]
    assert all(b == bodies[0] for b in bodies)
    assert sum(r.headers["Idempotent-Replayed"] == "false" for r in responses) == 1
    assert await n_entries(engine) == 1
    assert await balance(client, acct) == 250


async def test_replay_storm_500_requests_100_keys_no_duplicates(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    keys = [(key(), i + 1) for i in range(100)]
    work = [(k, amt) for k, amt in keys for _ in range(5)]
    random.Random(1234).shuffle(work)
    sem = asyncio.Semaphore(60)

    async def one(k: str, amt: int) -> tuple[str, Response]:
        async with sem:
            return k, await deposit(client, acct, amt, k)

    results = await asyncio.gather(*(one(k, a) for k, a in work))
    by_key: dict[str, list[dict[str, Any]]] = {}
    for k, r in results:
        assert r.status_code == 201, r.text
        by_key.setdefault(k, []).append(r.json())
    assert len(by_key) == 100
    for bodies in by_key.values():
        assert len(bodies) == 5
        assert all(b == bodies[0] for b in bodies)
    assert len({b[0]["id"] for b in by_key.values()}) == 100
    assert await n_entries(engine) == 100
    assert await scalar(engine, "SELECT count(DISTINCT entry_id) FROM idempotency_keys") == 100
    assert await balance(client, acct) == sum(a for _, a in keys)


async def test_business_error_is_not_cached_and_key_can_be_retried(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    k = key()
    body = {"account_id": acct, "amount_minor": 100, "currency": "USD"}
    assert_problem(await post(client, "/withdrawals", body, k), 422, "insufficient_funds")
    assert await scalar(engine, "SELECT count(*) FROM idempotency_keys WHERE key = :k", k=k) == 0
    assert (await deposit(client, acct, 100)).status_code == 201
    retry = await post(client, "/withdrawals", body, k)
    assert retry.status_code == 201, retry.text
    assert await balance(client, acct) == 0


async def test_crash_inside_work_rolls_back_entry_and_key(
    client: AsyncClient, sessionmaker: SM, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    entries_before = await n_entries(engine)

    class Crash(Exception):
        pass

    async def run() -> None:
        async with sessionmaker() as s, s.begin():

            async def work() -> WorkResult:
                await ledger.deposit(s, acct, Money(100, "USD"))  # entry IS posted ...
                raise Crash  # ... then we die before completion

            await run_idempotent(s, "crash-key", "fp", work)

    with pytest.raises(Crash):
        await run()
    assert await n_entries(engine) == entries_before
    assert (
        await scalar(engine, "SELECT count(*) FROM idempotency_keys WHERE key = 'crash-key'") == 0
    )
    assert await balance(client, acct) == 0


async def test_crash_between_post_and_completion_via_http_then_retry_succeeds(
    database_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(database_url)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        acct = await open_account(c)
        entries_before = await n_entries(engine)

        async def boom(*_: Any) -> None:
            raise RuntimeError("simulated crash before marking the key COMPLETED")

        monkeypatch.setattr(idem, "_complete", boom)
        k = key()
        r = await deposit(c, acct, 300, k)
        assert_problem(r, 500, "internal_error")
        assert await n_entries(engine) == entries_before
        n_keys = await scalar(engine, "SELECT count(*) FROM idempotency_keys WHERE key = :k", k=k)
        assert n_keys == 0

        monkeypatch.undo()
        retry = await deposit(c, acct, 300, k)
        assert retry.status_code == 201
        assert retry.headers["Idempotent-Replayed"] == "false"
        assert await n_entries(engine) == entries_before + 1
        assert await balance(c, acct) == 300


async def test_missing_and_invalid_idempotency_key(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    body = {"account_id": acct, "amount_minor": 1, "currency": "USD"}
    for path in ("/deposits", "/withdrawals"):
        assert_problem(await post(client, path, body, None), 400, "missing_idempotency_key")
    assert_problem(
        await post(client, "/accounts", {"currency": "USD"}, None), 400, "missing_idempotency_key"
    )
    tbody = {
        "from_account_id": acct,
        "to_account_id": acct + 1,
        "amount_minor": 1,
        "currency": "USD",
    }
    assert_problem(await post(client, "/transfers", tbody, None), 400, "missing_idempotency_key")
    r = await client.post("/entries/1/reverse", headers={})
    assert_problem(r, 400, "missing_idempotency_key")
    assert_problem(await post(client, "/deposits", body, ""), 400, "invalid_idempotency_key")
    assert_problem(await post(client, "/deposits", body, "x" * 256), 400, "invalid_idempotency_key")
    assert await n_entries(engine) == 0


# ---- money movement -----------------------------------------------------------------


async def test_concurrent_overdraw_through_api_never_goes_negative(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    acct = await open_account(client)
    assert (await deposit(client, acct, 1_000)).status_code == 201
    body = {"account_id": acct, "amount_minor": 100, "currency": "USD"}
    responses = await asyncio.gather(
        *(post(client, "/withdrawals", body, key()) for _ in range(50))
    )
    ok = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code != 201]
    assert len(ok) == 10
    for r in rejected:
        assert_problem(r, 422, "insufficient_funds")
    assert await balance(client, acct) == 0
    assert (
        await scalar(
            engine,
            "SELECT min(balance_minor) FROM balances b JOIN accounts a "
            "ON a.id = b.account_id WHERE NOT a.is_system",
        )
        == 0
    )


async def test_concurrent_transfers_overdraw_never_negative(client: AsyncClient) -> None:
    a = await open_account(client)
    b = await open_account(client)
    assert (await deposit(client, a, 500)).status_code == 201
    assert (await deposit(client, b, 500)).status_code == 201

    async def move(src: int, dst: int) -> Response:
        body = {"from_account_id": src, "to_account_id": dst, "amount_minor": 70, "currency": "USD"}
        return await post(client, "/transfers", body, key())

    responses = await asyncio.gather(*(move(a, b) if i % 2 else move(b, a) for i in range(60)))
    assert {r.status_code for r in responses} <= {201, 422}
    bal_a, bal_b = await balance(client, a), await balance(client, b)
    assert bal_a >= 0 and bal_b >= 0
    assert bal_a + bal_b == 1_000


async def test_transfer_list_entries_and_balance(client: AsyncClient) -> None:
    a = await open_account(client)
    b = await open_account(client)
    dep = await deposit(client, a, 1_000)
    body = {"from_account_id": a, "to_account_id": b, "amount_minor": 400, "currency": "USD"}
    t = await post(client, "/transfers", {**body, "description": "rent"}, key())
    assert t.status_code == 201, t.text
    tj = t.json()
    assert tj["entry_type"] == "TRANSFER" and tj["description"] == "rent"
    assert sorted((ln["account_id"], ln["amount_minor"]) for ln in tj["lines"]) == sorted(
        [(a, -400), (b, 400)]
    )
    assert await balance(client, a) == 600
    assert await balance(client, b) == 400

    page = (await client.get(f"/accounts/{a}/entries")).json()
    assert page["account_id"] == a
    assert [e["id"] for e in page["entries"]] == [tj["id"], dep.json()["id"]]
    assert page["next_before_id"] is None
    first = (await client.get(f"/accounts/{a}/entries", params={"limit": 1})).json()
    assert [e["id"] for e in first["entries"]] == [tj["id"]]
    rest = (
        await client.get(
            f"/accounts/{a}/entries", params={"limit": 1, "before_id": first["next_before_id"]}
        )
    ).json()
    assert [e["id"] for e in rest["entries"]] == [dep.json()["id"]]

    same = {**body, "to_account_id": a}
    assert_problem(await post(client, "/transfers", same, key()), 422, "same_account_transfer")


async def test_system_account_is_single_per_currency_and_protected(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    accts = [await open_account(client, "EUR") for _ in range(10)]
    responses = await asyncio.gather(
        *(
            post(
                client, "/deposits", {"account_id": x, "amount_minor": 10, "currency": "EUR"}, key()
            )
            for x in accts
        )
    )
    assert {r.status_code for r in responses} == {201}
    n_sys = await scalar(
        engine, "SELECT count(*) FROM accounts WHERE is_system AND currency = 'EUR'"
    )
    assert n_sys == 1
    sys_id = await scalar(engine, "SELECT id FROM accounts WHERE is_system AND currency = 'EUR'")
    assert await balance(client, sys_id) == -100

    steal = {
        "from_account_id": sys_id,
        "to_account_id": accts[0],
        "amount_minor": 5,
        "currency": "EUR",
    }
    assert_problem(
        await post(client, "/transfers", steal, key()), 422, "system_account_not_allowed"
    )
    wd = {"account_id": sys_id, "amount_minor": 5, "currency": "EUR"}
    assert_problem(await post(client, "/withdrawals", wd, key()), 422, "system_account_not_allowed")


async def test_currency_mismatch(client: AsyncClient) -> None:
    acct = await open_account(client, "USD")
    body = {"account_id": acct, "amount_minor": 10, "currency": "EUR"}
    assert_problem(await post(client, "/deposits", body, key()), 422, "currency_mismatch")


# ---- reversal -----------------------------------------------------------------------


async def test_reversal_endpoint(client: AsyncClient, engine: AsyncEngine) -> None:
    acct = await open_account(client)
    dep = (await deposit(client, acct, 800)).json()
    k = key()
    rev = await client.post(
        f"/entries/{dep['id']}/reverse",
        json={"description": "oops"},
        headers={"Idempotency-Key": k},
    )
    assert rev.status_code == 201, rev.text
    rj = rev.json()
    assert rj["entry_type"] == "REVERSAL"
    assert rj["reverses_entry_id"] == dep["id"]
    assert rj["description"] == "oops"
    assert sorted((ln["account_id"], ln["amount_minor"]) for ln in rj["lines"]) == sorted(
        (ln["account_id"], -ln["amount_minor"]) for ln in dep["lines"]
    )
    assert await balance(client, acct) == 0

    replay = await client.post(
        f"/entries/{dep['id']}/reverse",
        json={"description": "oops"},
        headers={"Idempotency-Key": k},
    )
    assert replay.json() == rj

    again = await client.post(f"/entries/{dep['id']}/reverse", headers={"Idempotency-Key": key()})
    body = assert_problem(again, 409, "entry_already_reversed")
    assert body["entry_id"] == dep["id"]

    missing = await client.post("/entries/999999/reverse", headers={"Idempotency-Key": key()})
    assert_problem(missing, 404, "unknown_entry")
    assert await n_entries(engine) == 2


async def test_reversal_that_would_overdraw_is_rejected(client: AsyncClient) -> None:
    a = await open_account(client)
    b = await open_account(client)
    dep = (await deposit(client, a, 100)).json()
    body = {"from_account_id": a, "to_account_id": b, "amount_minor": 100, "currency": "USD"}
    assert (await post(client, "/transfers", body, key())).status_code == 201
    r = await client.post(f"/entries/{dep['id']}/reverse", headers={"Idempotency-Key": key()})
    assert_problem(r, 422, "insufficient_funds")


async def test_concurrent_reversals_only_one_wins(client: AsyncClient, engine: AsyncEngine) -> None:
    acct = await open_account(client)
    dep = (await deposit(client, acct, 100)).json()
    responses = await asyncio.gather(
        *(
            client.post(f"/entries/{dep['id']}/reverse", headers={"Idempotency-Key": key()})
            for _ in range(20)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 19
    assert await n_entries(engine) == 2


# ---- validation / problem+json ------------------------------------------------------


@pytest.mark.parametrize(
    "amount",
    [10.0, 10.5, "10", True, 0, -5, None, 2**63],
)
async def test_amount_must_be_strict_positive_int(client: AsyncClient, amount: object) -> None:
    acct = await open_account(client)
    body = {"account_id": acct, "amount_minor": amount, "currency": "USD"}
    problem = assert_problem(await post(client, "/deposits", body, key()), 422, "validation_error")
    assert any(e["loc"][-1] == "amount_minor" for e in problem["errors"])


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": 10, "currency": "usd"},
        {"amount_minor": 10, "currency": "US"},
        {"amount_minor": 10, "currency": "USD", "unexpected": 1},
        {"amount_minor": 10},
        {"amount_minor": 10, "currency": "USD", "account_id": "1"},
    ],
)
async def test_body_validation_is_problem_json(client: AsyncClient, body: dict[str, Any]) -> None:
    acct = await open_account(client)
    full = {"account_id": acct, **body}
    assert_problem(await post(client, "/deposits", full, key()), 422, "validation_error")


async def test_unknown_account_problem_shape(client: AsyncClient) -> None:
    body = {"account_id": 424242, "amount_minor": 10, "currency": "USD"}
    problem = assert_problem(await post(client, "/deposits", body, key()), 404, "unknown_account")
    assert problem["account_ids"] == [424242]
    assert_problem(await client.get("/accounts/424242/balance"), 404, "unknown_account")
    assert_problem(await client.get("/accounts/424242/entries"), 404, "unknown_account")
    assert_problem(await client.get("/nope"), 404, "not_found")


async def test_open_account_is_idempotent(client: AsyncClient, engine: AsyncEngine) -> None:
    k = key()
    a = await post(client, "/accounts", {"currency": "GBP"}, k)
    b = await post(client, "/accounts", {"currency": "GBP"}, k)
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json()
    assert a.json()["currency"] == "GBP" and a.json()["is_system"] is False
    assert await scalar(engine, "SELECT count(*) FROM accounts") == 1


# ---- PENDING-twin semantics (deterministic, at the service level) --------------------


async def _twin_race(sessionmaker: SM, winner_fails: bool) -> tuple[Any, list[str]]:
    gate = asyncio.Event()
    claimed = asyncio.Event()
    ran: list[str] = []

    async def winner() -> None:
        async with sessionmaker() as s, s.begin():

            async def work() -> WorkResult:
                ran.append("winner")
                claimed.set()
                await gate.wait()
                if winner_fails:
                    raise RuntimeError("winner failed")
                return WorkResult(201, {"by": "winner"})

            await run_idempotent(s, "twin", "fp", work)

    async def loser() -> Any:
        async with sessionmaker() as s, s.begin():

            async def work() -> WorkResult:
                ran.append("loser")
                return WorkResult(201, {"by": "loser"})

            return await run_idempotent(s, "twin", "fp", work)

    w = asyncio.create_task(winner())
    await claimed.wait()
    lose = asyncio.create_task(loser())
    await asyncio.sleep(0.3)
    assert not lose.done(), "loser must block on the winner's uncommitted claim"
    gate.set()
    if winner_fails:
        with pytest.raises(RuntimeError):
            await w
    else:
        await w
    return await lose, ran


async def test_inflight_twin_blocks_then_replays_winner(sessionmaker: SM) -> None:
    outcome, ran = await _twin_race(sessionmaker, winner_fails=False)
    assert ran == ["winner"]
    assert outcome.replayed is True
    assert outcome.body == {"by": "winner"}


async def test_inflight_twin_takes_over_when_winner_rolls_back(sessionmaker: SM) -> None:
    outcome, ran = await _twin_race(sessionmaker, winner_fails=True)
    assert ran == ["winner", "loser"]
    assert outcome.replayed is False
    assert outcome.body == {"by": "loser"}
