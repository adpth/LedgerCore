from base64 import urlsafe_b64encode
from hashlib import sha256
from time import time
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from ledger.auth import require
    from ledger.console_auth import router

    env = {
        "CONSOLE_OIDC_ORIGIN": "https://ledger.test",
        "CONSOLE_OIDC_CLIENT_ID": "finance-console",
        "CONSOLE_OIDC_CLIENT_SECRET": "test-secret",
        "CONSOLE_OIDC_AUTHORIZE_URL": "https://issuer.test/authorize",
        "CONSOLE_OIDC_TOKEN_URL": "https://issuer.test/token",
        "CONSOLE_SESSION_SECRET": "x" * 32,
        "OAUTH_ISSUER": "https://issuer.test",
        "OAUTH_AUDIENCE": "ledger-api",
        "OAUTH_JWKS_URL": "https://issuer.test/jwks",
        "LEDGER_DEMO": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()
    app.include_router(router)

    @app.get("/read")
    def read(subject: Annotated[str, Depends(require("ledger:read"))]):
        return {"subject": subject}

    @app.post("/write")
    def write(subject: Annotated[str, Depends(require("ledger:transfer"))]):
        return {"subject": subject}

    return TestClient(app, base_url=env["CONSOLE_OIDC_ORIGIN"], follow_redirects=False)


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    ("changes", "target"),
    [
        ({}, "/"),
        ({"roles": ["viewer"]}, "/?sign_in=denied"),
        ({"nonce": "different"}, "/?sign_in=failed"),
        ({"aud": "ledger-api"}, "/?sign_in=failed"),
        ({"aud": ["finance-console", "ledger-api"]}, "/?sign_in=failed"),
        ({"iss": "https://another.test"}, "/?sign_in=failed"),
        ({"azp": "different"}, "/?sign_in=failed"),
        ({"exp": 1}, "/?sign_in=failed"),
    ],
)
def test_oidc_callback(client, monkeypatch, key, changes, target):
    from ledger import auth, console_auth

    response = client.get("/console/login")
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://ledger.test/console/callback"]

    claims = {
        "sub": "finance-person",
        "iss": "https://issuer.test",
        "aud": "finance-console",
        "nonce": query["nonce"][0],
        "exp": int(time()) + 300,
        "iat": int(time()),
        "roles": ["finance"],
        **changes,
    }
    id_token = jwt.encode(claims, key, algorithm="RS256")
    monkeypatch.setattr(
        auth,
        "jwks",
        lambda _: SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
        ),
    )
    codes = []

    def exchange(url, *, auth, data, timeout):
        assert url == "https://issuer.test/token"
        assert auth == ("finance-console", "test-secret")
        assert timeout == 5
        challenge = urlsafe_b64encode(sha256(data["code_verifier"].encode()).digest()).rstrip(b"=")
        assert challenge.decode() == query["code_challenge"][0]
        codes.append(data["code"])
        return httpx.Response(200, json={"id_token": id_token}, request=httpx.Request("POST", url))

    monkeypatch.setattr(console_auth.httpx, "post", exchange)
    assert (
        client.get("/console/callback?state=wrong&code=abc").headers["location"].endswith("failed")
    )
    assert not codes
    callback = f"/console/callback?state={query['state'][0]}&code=abc"
    response = client.get(callback)
    assert response.headers["location"] == target
    assert codes == ["abc"]
    assert client.get(callback).headers["location"].endswith("failed")
    assert codes == ["abc"]

    if target == "/":
        assert client.get("/console/session").json()["authenticated"]
        assert client.get("/read").json() == {"subject": "finance-person"}
        assert client.post("/write").status_code == 401
        assert client.get("/read", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert (
            client.post("/console/logout", headers={"Origin": "https://other.test"}).status_code
            == 403
        )
        assert client.get("/read").status_code == 200
        assert (
            client.post("/console/logout", headers={"Origin": "https://ledger.test"}).status_code
            == 204
        )
    assert client.get("/read").status_code == 401


def test_demo_sign_in_and_expired_session(client, monkeypatch):
    from ledger import console_auth

    origin = {"Origin": "https://ledger.test"}
    assert client.post("/console/demo", headers=origin).status_code == 404
    monkeypatch.setenv("LEDGER_DEMO", "1")
    monkeypatch.setenv("DEMO_JWT_SECRET", "x" * 32)
    assert client.post("/console/demo").status_code == 403
    assert client.post("/console/demo", headers=origin).status_code == 204
    assert client.get("/read").json() == {"subject": "demo-finance"}
    assert client.post("/write").status_code == 401
    expired = console_auth.seal("session", {"sub": "demo-finance"}, -1)
    client.cookies.set(console_auth.cookie_name("session"), expired, domain="ledger.test", path="/")
    assert not client.get("/console/session").json()["authenticated"]
    assert client.get("/read").status_code == 401


def test_missing_configuration_stays_signed_out(client, monkeypatch):
    monkeypatch.delenv("CONSOLE_SESSION_SECRET")
    assert not client.get("/console/session").json()["enabled"]
    assert client.get("/console/login").headers["location"] == "/?sign_in=unavailable"
    assert client.get("/read").status_code == 401
