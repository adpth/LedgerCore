import hashlib
import json
from collections.abc import Callable
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as Transaction

from ledger.db import Account, Balance, Entry, Idempotency, Line, Outbox, Session


def problem(status: int, message: str) -> HTTPException:
    return HTTPException(status, message)


def once(
    subject: str,
    key: str,
    operation: str,
    body: dict,
    execute: Callable[[Transaction], dict],
) -> dict:
    encoded = json.dumps([operation, body], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with Session.begin() as db:
        claimed = db.scalar(
            insert(Idempotency)
            .values(subject=subject, key=key, digest=digest, status="PENDING")
            .on_conflict_do_nothing()
            .returning(Idempotency.key)
        )
        request = db.get_one(Idempotency, (subject, key))
        if claimed is None:
            if request.digest != digest:
                raise problem(422, "Idempotency key already used with another request")
            assert request.status == "COMPLETED" and request.response is not None
            return request.response
        result = execute(db)
        request.status = "COMPLETED"
        request.response = result
        return result


def accounts_for(db: Transaction, roles: dict[int, str]) -> dict[int, Account]:
    accounts = db.scalars(
        select(Account).where(Account.id.in_(roles)).order_by(Account.id).with_for_update()
    ).all()
    if len(accounts) != len(roles):
        raise problem(404, "Account not found")
    if any(a.kind != roles[a.id] for a in accounts):
        raise problem(422, "Account type does not match the operation")
    if len({a.currency for a in accounts}) != 1:
        raise problem(422, "Accounts must use the same currency")
    return {a.id: a for a in accounts}


def post(
    db: Transaction,
    kind: str,
    source: int,
    destination: int,
    amount: int,
    source_kind: str,
    destination_kind: str,
    reference: str = "",
    reversal_of: int | None = None,
) -> dict:
    lines = [(source, -amount), (destination, amount)]
    if source == destination or amount <= 0 or sum(delta for _, delta in lines) != 0:
        raise problem(422, "Entry must contain distinct accounts and balanced nonzero lines")
    accounts = accounts_for(db, {source: source_kind, destination: destination_kind})
    entry = Entry(
        kind=kind, currency=accounts[source].currency, reference=reference, reversal_of=reversal_of
    )
    db.add(entry)
    db.flush()
    for account_id, delta in lines:
        balance = db.get_one(Balance, account_id)
        updated = int(balance.amount) + delta
        if updated < 0 and accounts[account_id].kind != "clearing":
            raise problem(409, "Insufficient available funds")
        balance.amount = Decimal(updated)
        db.add(
            Line(entry_id=entry.id, account_id=account_id, amount=delta, currency=entry.currency)
        )
    result = {
        "id": str(entry.id),
        "kind": kind,
        "currency": entry.currency,
        "reference": reference,
        "created_at": entry.created_at.isoformat(),
        "reversal_of": str(reversal_of) if reversal_of else None,
        "lines": [{"account_id": str(i), "amount_minor": str(n)} for i, n in lines],
    }
    db.add(Outbox(id=entry.id, payload={"event_id": f"ledger.entry.{entry.id}", **result}))
    return result


def drift(db: Transaction) -> list[dict]:
    rows = db.execute(
        text("""
        SELECT a.id, b.amount AS projected, coalesce(sum(l.amount), 0) AS journal
        FROM accounts a LEFT JOIN balances b ON b.account_id = a.id
        LEFT JOIN lines l ON l.account_id = a.id
        GROUP BY a.id, b.amount HAVING b.amount IS DISTINCT FROM coalesce(sum(l.amount), 0)
        ORDER BY a.id
    """)
    )
    return [
        {
            "account_id": str(row.id),
            "projected": str(row.projected) if row.projected is not None else None,
            "journal": str(row.journal),
        }
        for row in rows
    ]
