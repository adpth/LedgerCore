import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def database():
    if url := os.getenv("LEDGER_TEST_DATABASE_URL"):
        yield url
    else:
        with PostgresContainer("postgres:17", driver="psycopg") as postgres:
            yield postgres.get_connection_url()


@pytest.fixture(scope="session")
def api(database):
    with pytest.MonkeyPatch.context() as env:
        env.setenv("DATABASE_URL", database)
        env.setenv("LEDGER_DEMO", "1")
        env.setenv("DEMO_JWT_SECRET", "integration-ledgercore-signing-key")
        env.setenv("DEMO_CLIENT_SECRET", "test-secret")
        command.upgrade(Config("alembic.ini"), "head")
        from ledger.api import app

        yield TestClient(app)


def headers(api, subject="wallet-service"):
    result = api.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": subject,
            "client_secret": "test-secret",
        },
    )
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["access_token"]}


def request(api, path, auth, body=None, key=None):
    return api.post(
        "/api/" + path,
        json=body,
        headers={
            **auth,
            "Idempotency-Key": key or str(uuid4()),
        },
    )


@pytest.fixture
def accounts(api):
    auth = headers(api)
    result = []
    for kind in ("clearing", "wallet", "wallet", "reserve"):
        response = request(api, "accounts", auth, {"name": kind, "currency": "USD", "kind": kind})
        assert response.status_code == 201, response.text
        result.append(int(response.json()["id"]))
    clearing, wallet, _, _ = result
    deposit = request(
        api,
        "movements/deposit",
        auth,
        {"source": clearing, "destination": wallet, "amount_minor": 100},
    )
    assert deposit.status_code == 201, deposit.text
    return result


def concurrent(function):
    barrier = Barrier(2)

    def run(index):
        barrier.wait(timeout=5)
        return function(index)

    with ThreadPoolExecutor(2) as pool:
        return list(pool.map(run, range(2)))


def test_replay_is_atomic(api, accounts):
    from ledger.db import Session

    auth = headers(api)
    _, source, destination, _ = accounts
    body = {"source": source, "destination": destination, "amount_minor": 60}
    key = str(uuid4())
    first, second = concurrent(lambda _: request(api, "movements/transfer", auth, body, key))
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert (
        request(api, "movements/transfer", auth, {**body, "amount_minor": 1}, key).status_code
        == 422
    )
    with Session() as db:
        assert (
            db.scalar(
                text("SELECT count(*) FROM outbox WHERE id=:id"), {"id": int(first.json()["id"])}
            )
            == 1
        )


def test_overdraft_and_rollback(api, accounts):
    auth = headers(api)
    clearing, source, destination, _ = accounts
    body = {"source": source, "destination": destination, "amount_minor": 80}
    keys = [str(uuid4()), str(uuid4())]
    results = concurrent(lambda i: request(api, "movements/transfer", auth, body, keys[i]))
    assert sorted(r.status_code for r in results) == [201, 409]
    failed = next(i for i, result in enumerate(results) if result.status_code == 409)
    assert (
        request(
            api,
            "movements/deposit",
            auth,
            {"source": clearing, "destination": source, "amount_minor": 60},
        ).status_code
        == 201
    )
    assert request(api, "movements/transfer", auth, body, keys[failed]).status_code == 201
    assert api.get("/api/reconciliation", headers=auth).json() == {"mismatches": []}


def test_database_journal_guards(api, accounts):
    from ledger.db import Session

    clearing, source, destination, _ = accounts
    with pytest.raises(DBAPIError, match="must have balanced lines"), Session.begin() as db:
        entry = db.scalar(
            text("""
            INSERT INTO entries (kind, currency, reference) VALUES ('test', 'USD', '')
            RETURNING id
        """)
        )
        db.execute(
            text("INSERT INTO lines VALUES (:entry, :account, 'USD', 1)"),
            {"entry": entry, "account": source},
        )
    with Session() as db:
        original = db.scalar(
            text("SELECT entry_id FROM lines WHERE account_id=:id"), {"id": source}
        )
    with pytest.raises(DBAPIError, match="sealed"), Session.begin() as db:
        db.execute(
            text("INSERT INTO lines VALUES (:entry, :account, 'USD', 1)"),
            {"entry": original, "account": destination},
        )
    with pytest.raises(DBAPIError, match="append-only"), Session.begin() as db:
        db.execute(text("DELETE FROM lines WHERE account_id=:id"), {"id": clearing})


def test_reversal_finance_and_export(api, accounts):
    auth, finance = headers(api), headers(api, "finance")
    _, source, destination, _ = accounts
    body = {"source": source, "destination": destination, "amount_minor": 30, "reference": "=1+1"}
    assert request(api, "movements/transfer", finance, body).status_code == 403
    posted = request(api, "movements/transfer", auth, body).json()
    path = f"entries/{posted['id']}/reverse"
    reversal = request(api, path, auth)
    assert reversal.status_code == 201
    assert reversal.json()["reversal_of"] == posted["id"]
    assert request(api, path, auth).status_code == 409
    response = api.get("/api/export?kind=transfer", headers=finance)
    assert response.status_code == 200 and "'=1+1" in response.text
    page = api.get("/api/entries?limit=1", headers=finance).json()
    next_page = api.get(f"/api/entries?limit=1&before={page['next']}", headers=finance).json()
    assert page["items"][0]["id"] != next_page["items"][0]["id"]
    assert api.get("/api/accounts").status_code == 401


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("succeeded", "COMPLETED"),
        ("declined", "DECLINED"),
    ],
)
def test_payout_saga(api, accounts, monkeypatch, result, expected):
    from ledger.worker import settle

    clearing, source, _, reserve = accounts
    auth = headers(api)
    response = request(
        api,
        "payouts",
        auth,
        {
            "source": source,
            "destination": reserve,
            "clearing": clearing,
            "provider_destination": "merchant",
            "amount_minor": 50,
        },
    )
    assert response.status_code == 202, response.text
    hold = response.json()["hold"]
    path = f"/api/payouts/{hold['id']}"
    assert api.get(path, headers=auth).json()["status"] == "PENDING"
    monkeypatch.setenv("PAYOUT_URL", "http://payout.test/payout")

    def provider(call):
        assert call.headers["Idempotency-Key"] == f"ledger-payout-{hold['id']}"
        return httpx.Response(200, json={"status": result})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        assert settle(client)
    payout = api.get(path, headers=auth).json()
    assert payout["status"] == expected and payout["final_entry"]
    assert api.get("/api/reconciliation", headers=auth).json() == {"mismatches": []}


def test_payout_releases_into_wallet_above_int64(api, accounts, monkeypatch):
    from ledger.db import Payout, Session
    from ledger.worker import settle

    clearing, source, _, reserve = accounts
    auth = headers(api)
    hold = request(
        api,
        "payouts",
        auth,
        {
            "source": source,
            "destination": reserve,
            "clearing": clearing,
            "provider_destination": "merchant",
            "amount_minor": 50,
        },
    ).json()["hold"]
    assert (
        request(
            api,
            "movements/deposit",
            auth,
            {"source": clearing, "destination": source, "amount_minor": 2**63 - 1},
        ).status_code
        == 201
    )
    monkeypatch.setenv("PAYOUT_URL", "http://payout.test/payout")

    seen = []

    def provider(call):
        seen.append(call.headers["Idempotency-Key"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("Simulated timeout", request=call)
        return httpx.Response(200, json={"status": "declined"})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        assert settle(client)
        assert not settle(client)
        with Session.begin() as db:
            db.get_one(Payout, int(hold["id"])).retry_at = datetime.now(UTC)
        assert settle(client)
    assert len(set(seen)) == 1
    available = next(
        row["balance_minor"]
        for row in api.get("/api/accounts", headers=auth).json()
        if row["id"] == str(source)
    )
    assert int(available) == 2**63 - 1 + 100
    assert api.get("/api/reconciliation", headers=auth).json() == {"mismatches": []}


def test_reconciliation_reports_drift(api, accounts):
    from ledger.db import Balance, Session

    _, source, _, _ = accounts
    try:
        with Session.begin() as db:
            db.get_one(Balance, source).amount += 1
        mismatches = api.get("/api/reconciliation", headers=headers(api)).json()["mismatches"]
        assert {"account_id": str(source), "projected": "101", "journal": "100"} in mismatches
    finally:
        with Session.begin() as db:
            db.get_one(Balance, source).amount -= 1


def test_oauth_client_credentials_and_production_jwt(api, monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from ledger.auth import jwks

    token_response = api.post(
        "/oauth/token", auth=("finance", "test-secret"), data={"grant_type": "client_credentials"}
    )
    assert token_response.status_code == 200
    assert token_response.headers["cache-control"] == "no-store"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    issuer = "https://issuer.test"
    jwks_url = issuer + "/jwks"
    monkeypatch.delenv("LEDGER_DEMO")
    monkeypatch.setenv("OAUTH_ISSUER", issuer)
    monkeypatch.setenv("OAUTH_AUDIENCE", "ledgercore")
    monkeypatch.setenv("OAUTH_JWKS_URL", jwks_url)
    monkeypatch.setattr(jwks(jwks_url), "fetch_data", lambda: {"keys": [public_jwk]})
    claims = {
        "sub": "finance-user",
        "iss": issuer,
        "aud": "ledgercore",
        "roles": ["finance"],
        "exp": int(datetime.now(UTC).timestamp()) + 60,
    }

    def signed(overrides):
        token = jwt.encode(
            {**claims, **overrides}, private_key, algorithm="RS256", headers={"kid": "test-key"}
        )
        return {"Authorization": "Bearer " + token}

    assert api.get("/api/accounts", headers=signed({})).status_code == 200
    assert api.get("/api/accounts", headers=signed({"aud": "another-service"})).status_code == 401
    assert (
        request(
            api,
            "accounts",
            signed({"scope": "ledger:transfer"}),
            {"name": "Test", "currency": "USD", "kind": "wallet"},
        ).status_code
        == 403
    )


def test_checkpoint_avoids_repeating_terminal_provider_call(api, accounts, monkeypatch):
    from ledger import worker
    from ledger.db import Session

    clearing, wallet, _, reserve = accounts
    auth = headers(api)
    created = request(
        api,
        "payouts",
        auth,
        {
            "source": wallet,
            "destination": reserve,
            "clearing": clearing,
            "provider_destination": "merchant",
            "amount_minor": 50,
        },
    )
    assert created.status_code == 202, created.text
    payout_id = created.json()["hold"]["id"]
    monkeypatch.setenv("PAYOUT_URL", "http://payout.test/payout")
    provider_calls = []

    def provider(call):
        provider_calls.append(call.headers["Idempotency-Key"])
        return httpx.Response(200, json={"status": "succeeded"})

    def interrupted(*args, **kwargs):
        raise RuntimeError("Closing entry interrupted")

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(worker, "post", interrupted)
            with pytest.raises(RuntimeError, match="interrupted"):
                worker.settle(client)
        with Session() as db:
            state = db.execute(
                text("SELECT provider_status, status, final_entry FROM payouts WHERE id=:id"),
                {"id": int(payout_id)},
            ).one()
        assert tuple(state) == ("succeeded", "PENDING", None)
        assert not worker.settle(client)
        with Session.begin() as db:
            db.execute(
                text("UPDATE payouts SET retry_at=now() WHERE id=:id"), {"id": int(payout_id)}
            )
        assert worker.settle(client)

    assert provider_calls == [f"ledger-payout-{payout_id}"]
    assert api.get(f"/api/payouts/{payout_id}", headers=auth).json()["status"] == "COMPLETED"
    assert api.get("/api/reconciliation", headers=auth).json() == {"mismatches": []}


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "0.5"])
def test_balance_guard(api, accounts, amount):
    from ledger.db import Session

    with pytest.raises(DBAPIError) as raised, Session.begin() as db:
        db.execute(
            text("UPDATE balances SET amount=CAST(:amount AS numeric) WHERE account_id=:id"),
            {"amount": amount, "id": accounts[1]},
        )
    assert raised.value.orig.sqlstate == "23514"


def test_invalid_text_and_filters(api):
    auth = headers(api)
    result = request(
        api, "accounts", auth, {"name": "bad\x00name", "currency": "USD", "kind": "wallet"}
    )
    assert result.status_code == 422
    assert "input" not in result.json()["detail"][0]
    result = api.post(
        "/api/accounts",
        headers={**auth, "Idempotency-Key": str(uuid4()), "Content-Type": "application/json"},
        content=rb'{"name":"\ud800","currency":"USD","kind":"wallet"}',
    )
    assert result.status_code == 422
    for path in ("entries", "export"):
        assert api.get(f"/api/{path}?kind=bad%00", headers=auth).status_code == 422


def test_oversized_request(api):
    response = api.post("/oauth/token", content=b"x" * (16 * 1024 + 1))
    assert response.status_code == 413


def test_opposite_transfers_and_concurrent_reversal(api, accounts):
    auth = headers(api)
    clearing, source, destination, _ = accounts
    assert (
        request(
            api,
            "movements/deposit",
            auth,
            {
                "source": clearing,
                "destination": destination,
                "amount_minor": 100,
            },
        ).status_code
        == 201
    )
    pairs = [(source, destination), (destination, source)]
    posted = concurrent(
        lambda i: request(
            api,
            "movements/transfer",
            auth,
            {
                "source": pairs[i][0],
                "destination": pairs[i][1],
                "amount_minor": 50,
            },
        )
    )
    assert [result.status_code for result in posted] == [201, 201]
    entry_id = posted[0].json()["id"]
    reversals = concurrent(lambda _: request(api, f"entries/{entry_id}/reverse", auth))
    assert sorted(result.status_code for result in reversals) == [201, 409]
    assert api.get("/api/reconciliation", headers=auth).json() == {"mismatches": []}


def test_outbox_ack_failure_leaves_pending_event(api, accounts):
    from ledger.db import Outbox, Session
    from ledger.worker import relay

    class Broker:
        def __init__(self):
            self.error = RuntimeError("broker unavailable")
            self.keys = []

        def produce(self, _topic, *, key, value, on_delivery):
            self.keys.append(key)
            on_delivery(self.error, None)

        def flush(self, _timeout):
            return 0

    broker = Broker()
    with pytest.raises(RuntimeError, match="broker unavailable"):
        relay(broker, "test.entries")
    event_id = int(broker.keys[0].split(".")[-1])
    with Session() as db:
        assert db.get_one(Outbox, event_id).sent_at is None
    broker.error = None
    assert relay(broker, "test.entries")
    assert broker.keys[0] == broker.keys[1]
    with Session() as db:
        assert db.get_one(Outbox, event_id).sent_at is not None


def test_database_unavailable_has_retryable_response(api, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from ledger import api as module

    def unavailable():
        raise OperationalError(None, None, RuntimeError("database unavailable"))

    auth = headers(api)
    monkeypatch.setattr(module, "Session", unavailable)
    assert api.get("/health/live").status_code == 200
    assert api.get("/health/ready").status_code == 503
    for path in ("accounts", "export"):
        response = api.get(f"/api/{path}", headers=auth)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
