import os
import time
from hmac import compare_digest
from typing import Annotated
from urllib.parse import unquote_plus

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ledger.db import Account, Balance, Session
from ledger.service import post

IDENTITIES: dict[str, dict[str, str | list[str]]] = {
    "finance": {"roles": ["finance"]},
    "wallet-service": {"scope": "ledger:read ledger:transfer"},
}


def token(subject: str) -> str:
    return jwt.encode(
        {
            "sub": subject,
            "iss": "ledgercore-demo",
            "aud": "ledgercore",
            "exp": int(time.time()) + 3600,
            **IDENTITIES[subject],
        },
        os.environ["DEMO_JWT_SECRET"],
        algorithm="HS256",
    )


def seed():
    from sqlalchemy import select, text

    if os.getenv("LEDGER_DEMO") != "1":
        raise SystemExit("Enable LEDGER_DEMO to seed sample activity")
    with Session.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(420181)"))
        if db.scalar(select(Account.id).limit(1)):
            return
        names = [
            ("Settlement clearing", "clearing"),
            ("Operating wallet", "wallet"),
            ("Merchant wallet", "wallet"),
            ("Marketplace wallet", "wallet"),
            ("Payout reserve", "reserve"),
        ]
        accounts = [Account(name=name, kind=kind, currency="USD") for name, kind in names]
        db.add_all(accounts)
        db.flush()
        for account in accounts:
            db.add(Balance(account_id=account.id))
        db.flush()
        clearing, operating, merchant, marketplace, _ = [a.id for a in accounts]
        for wallet, amount, ref in [
            (operating, 16800000, "Opening settlement"),
            (merchant, 5834000, "Merchant funding"),
            (marketplace, 2270000, "Marketplace funding"),
        ]:
            post(db, "deposit", clearing, wallet, amount, "clearing", "wallet", ref)
        post(db, "transfer", operating, merchant, 125000, "wallet", "wallet", "Merchant allocation")
        post(db, "withdrawal", merchant, clearing, 84000, "wallet", "clearing", "Settlement batch")
        post(db, "transfer", marketplace, operating, 245000, "wallet", "wallet", "Wallet sweep")


provider = FastAPI()


@provider.post("/payout")
def simulate(body: dict):
    return {"status": "declined" if body["destination"] == "decline" else "succeeded"}


def register(app: FastAPI):
    basic = HTTPBasic(auto_error=False)

    @app.post("/oauth/token")
    def demo_oauth(
        response: Response,
        credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)],
        grant_type: str = Form(),
        client_id: str = Form(""),
        client_secret: str = Form(""),
    ):
        if credentials:
            if client_id or client_secret:
                raise HTTPException(400, "Supply one client authentication method")
            client_id = unquote_plus(credentials.username)
            client_secret = unquote_plus(credentials.password)
        expected = os.environ.get("DEMO_CLIENT_SECRET", "").encode()
        if (
            grant_type != "client_credentials"
            or client_id not in IDENTITIES
            or not expected
            or not compare_digest(client_secret.encode(), expected)
        ):
            raise HTTPException(401, "Invalid demo client credentials")
        response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
        return {"access_token": token(client_id), "token_type": "bearer", "expires_in": 3600}


if __name__ == "__main__":
    seed()
