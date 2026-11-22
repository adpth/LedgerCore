import csv
import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry import metrics
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError
from sqlalchemy.orm import Session as DatabaseSession

from ledger.auth import require
from ledger.console_auth import router as console_router
from ledger.db import Account, Balance, Entry, Line, Outbox, Payout, Session
from ledger.http import LimitRequestBody
from ledger.service import accounts_for, drift, once, post, problem

app = FastAPI(title="LedgerCore")
app.add_middleware(LimitRequestBody)
app.include_router(console_router)
static = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=static), name="assets")
Read = Annotated[str, Depends(require("ledger:read"))]
Write = Annotated[str, Depends(require("ledger:transfer"))]
Key = Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[!-~]{1,128}$")]
ID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Amount = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError) -> Response:
    detail = [{field: error[field] for field in ("loc", "msg", "type")} for error in exc.errors()]
    return Response(json.dumps({"detail": detail}), status_code=422, media_type="application/json")


@app.exception_handler(OperationalError)
@app.exception_handler(TimeoutError)
async def unavailable(_request: Request, _exc: Exception) -> Response:
    return Response(
        '{"detail":"Ledger database temporarily unavailable"}',
        status_code=503,
        media_type="application/json",
        headers={"Retry-After": "5"},
    )


@app.middleware("http")
async def prevent_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/", "/console/", "/oauth/")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def oldest_event(_options):
    with Session() as db:
        timestamp = db.scalar(select(func.min(Outbox.created_at)).where(Outbox.sent_at.is_(None)))
    age = (datetime.now(UTC) - timestamp).total_seconds() if timestamp else 0
    return [metrics.Observation(max(0, age))]


metrics.get_meter(__name__).create_observable_gauge(
    "ledger.outbox.oldest.pending", [oldest_event], unit="s"
)


def database_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid Unicode text") from exc
    return value


DatabaseText = Annotated[str, AfterValidator(database_text)]
EntryKind = Literal[
    "deposit",
    "withdrawal",
    "transfer",
    "reversal",
    "payout_hold",
    "payout_post",
    "payout_release",
]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountBody(Body):
    name: DatabaseText = Field(min_length=1, max_length=100, pattern=r"\S")
    currency: Currency
    kind: Literal["wallet", "clearing", "reserve"]


class Movement(Body):
    source: ID
    destination: ID
    amount_minor: Amount
    reference: DatabaseText = Field(default="", max_length=160)


class PayoutBody(Movement):
    clearing: ID
    provider_destination: DatabaseText = Field(min_length=1, max_length=160, pattern=r"\S")


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(
        static / "index.html",
        headers={
            "Content-Security-Policy": "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/health/live", include_in_schema=False)
def live():
    return {"status": "live"}


@app.get("/health/ready")
def ready():
    try:
        with Session() as db:
            db.execute(select(Payout.provider_status).limit(1))
    except SQLAlchemyError as exc:
        raise problem(503, "Ledger database unavailable") from exc
    return {"status": "ready"}


@app.get("/api/config")
def config():
    return {"demo": os.getenv("LEDGER_DEMO") == "1"}


@app.post("/api/accounts", status_code=201)
def create_account(body: AccountBody, subject: Write, key: Key):
    def create(db):
        account = Account(**body.model_dump())
        db.add(account)
        db.flush()
        db.add(Balance(account_id=account.id))
        return {"id": str(account.id), **body.model_dump()}

    return once(subject, key, "account", body.model_dump(), create)


@app.post("/api/movements/{kind}", status_code=201)
def movement(
    kind: Literal["deposit", "withdrawal", "transfer"], body: Movement, subject: Write, key: Key
):
    roles = {
        "deposit": ("clearing", "wallet"),
        "withdrawal": ("wallet", "clearing"),
        "transfer": ("wallet", "wallet"),
    }
    return once(
        subject,
        key,
        kind,
        body.model_dump(),
        lambda db: post(
            db,
            kind,
            body.source,
            body.destination,
            body.amount_minor,
            *roles[kind],
            body.reference,
        ),
    )


@app.post("/api/entries/{entry_id}/reverse", status_code=201)
def reverse(entry_id: int, subject: Write, key: Key):
    if not 0 < entry_id < 2**63:
        raise problem(422, "Invalid entry ID")

    def reverse_entry(db):
        original = db.scalar(select(Entry).where(Entry.id == entry_id).with_for_update())
        if original is None:
            raise problem(404, "Entry not found")
        if original.kind not in {"transfer", "deposit", "withdrawal"}:
            raise problem(422, "Only posted wallet movements can be reversed")
        if db.scalar(select(Entry.id).where(Entry.reversal_of == entry_id)):
            raise problem(409, "Entry has already been reversed")
        rows = db.execute(
            select(Line, Account.kind)
            .join(Account, Account.id == Line.account_id)
            .where(Line.entry_id == entry_id)
            .order_by(Line.amount.desc())
        ).all()
        if len(rows) != 2:
            raise problem(422, "Reversal requires a two-line movement")
        (source, source_kind), (target, target_kind) = rows
        return post(
            db,
            "reversal",
            source.account_id,
            target.account_id,
            source.amount,
            source_kind,
            target_kind,
            f"Reversal of {entry_id}",
            entry_id,
        )

    return once(subject, key, "reversal", {"entry_id": entry_id}, reverse_entry)


@app.post("/api/payouts", status_code=202)
def payout(body: PayoutBody, subject: Write, key: Key):
    def reserve(db):
        if len({body.source, body.destination, body.clearing}) != 3:
            raise problem(422, "Payout accounts must be distinct")
        accounts_for(
            db, {body.source: "wallet", body.destination: "reserve", body.clearing: "clearing"}
        )
        entry = post(
            db,
            "payout_hold",
            body.source,
            body.destination,
            body.amount_minor,
            "wallet",
            "reserve",
            body.reference,
        )
        db.add(
            Payout(
                id=int(entry["id"]),
                wallet=body.source,
                reserve=body.destination,
                clearing=body.clearing,
                amount=body.amount_minor,
                destination=body.provider_destination,
            )
        )
        return {"status": "PENDING", "hold": entry}

    return once(subject, key, "payout", body.model_dump(), reserve)


@app.get("/api/payouts/{payout_id}")
def payout_status(payout_id: int, _: Read):
    if not 0 < payout_id < 2**63:
        raise problem(422, "Invalid payout ID")
    with Session() as db:
        record = db.get(Payout, payout_id)
        if record is None:
            raise problem(404, "Payout not found")
        return {
            "id": str(record.id),
            "status": record.status,
            "final_entry": str(record.final_entry) if record.final_entry else None,
        }


@app.get("/api/accounts")
def list_accounts(_: Read):
    with Session() as db:
        return [
            {
                "id": str(a.id),
                "name": a.name,
                "currency": a.currency,
                "kind": a.kind,
                "balance_minor": str(amount),
            }
            for a, amount in db.execute(
                select(Account, Balance.amount).join(Balance).order_by(Account.id)
            )
        ]


def entry_query(kind: str | None, currency: str | None):
    query = select(
        Entry.id, Entry.kind, Entry.currency, Entry.reference, Entry.created_at, Entry.reversal_of
    )
    if kind:
        query = query.where(Entry.kind == kind)
    if currency:
        query = query.where(Entry.currency == currency)
    return query


@app.get("/api/entries")
def list_entries(
    _: Read,
    kind: EntryKind | None = None,
    currency: Currency | None = None,
    before: Annotated[int | None, Query(gt=0, le=2**63 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
):
    query = entry_query(kind, currency)
    if before is not None:
        query = query.where(Entry.id < before)
    with Session() as db:
        entries = db.execute(query.order_by(Entry.id.desc()).limit(limit + 1)).all()
        items = {
            row.id: {
                **row._asdict(),
                "id": str(row.id),
                "reversal_of": str(row.reversal_of) if row.reversal_of else None,
                "lines": [],
            }
            for row in entries[:limit]
        }
        for line, name in db.execute(
            select(Line, Account.name)
            .join(Account, Account.id == Line.account_id)
            .where(Line.entry_id.in_(items))
            .order_by(Line.amount)
        ):
            items[line.entry_id]["lines"].append(
                {
                    "account_id": str(line.account_id),
                    "account": name,
                    "amount_minor": str(line.amount),
                }
            )
        return {
            "items": list(items.values()),
            "next": str(entries[limit - 1].id) if len(entries) > limit else None,
        }


def csv_cell(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_session():
    with Session() as db:
        yield db


@app.get("/api/export")
def export(
    _: Read,
    db: Annotated[DatabaseSession, Depends(export_session)],
    kind: EntryKind | None = None,
    currency: Currency | None = None,
):
    entries = entry_query(kind, currency).subquery()
    query = (
        select(entries, Line.account_id, Account.name, Line.amount)
        .join(
            Line,
            Line.entry_id == entries.c.id,
        )
        .join(Account, Account.id == Line.account_id)
        .order_by(entries.c.id, Line.account_id)
    )

    rows = db.execute(query.execution_options(yield_per=500))

    def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "entry_id",
                "type",
                "currency",
                "reference",
                "posted_at",
                "reversal_of",
                "account_id",
                "account",
                "amount_minor",
            ]
        )
        yield buffer.getvalue()
        for row in rows:
            buffer.seek(0)
            buffer.truncate()
            writer.writerow(
                [
                    row.id,
                    row.kind,
                    row.currency,
                    csv_cell(row.reference),
                    row.created_at.isoformat(),
                    row.reversal_of,
                    row.account_id,
                    csv_cell(row.name),
                    row.amount,
                ]
            )
            yield buffer.getvalue()

    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=journal.csv"},
    )


@app.get("/api/overview")
def overview(_: Read):
    with Session() as db:
        totals = db.execute(
            select(Account.currency, func.sum(Balance.amount))
            .join(Balance)
            .where(Account.kind == "wallet")
            .group_by(Account.currency)
            .order_by(Account.currency)
        )
        return {
            "wallet_totals": {currency: str(amount) for currency, amount in totals},
            "wallets": db.scalar(
                select(func.count()).select_from(Account).where(Account.kind == "wallet")
            ),
            "entries": db.scalar(select(func.count()).select_from(Entry)),
            "pending_events": db.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.sent_at.is_(None))
            ),
        }


@app.get("/api/reconciliation")
def reconciliation(_: Read):
    with Session() as db:
        return {"mismatches": drift(db)}


if os.getenv("LEDGER_DEMO") == "1":
    from ledger.demo import register

    register(app)
