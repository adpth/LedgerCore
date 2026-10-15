"""HTTP routes. Every mutating POST requires an ``Idempotency-Key`` header."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ledgercore.api.deps import IdempotencyKeyDep, SessionDep
from ledgercore.api.schemas import (
    BIGINT_MAX,
    AccountOut,
    BalanceOut,
    DepositRequest,
    EntryOut,
    EntryPage,
    OpenAccountRequest,
    ReverseRequest,
    TransferRequest,
    WithdrawalRequest,
)
from ledgercore.domain import Money, request_fingerprint
from ledgercore.services import ledger
from ledgercore.services.idempotency import WorkResult, run_idempotent

router = APIRouter()

REPLAY_HEADER = "Idempotent-Replayed"

PathId = Annotated[int, Path(gt=0, le=BIGINT_MAX)]

_PROBLEM: dict[str, Any] = {"content": {"application/problem+json": {}}}
_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "Missing/invalid Idempotency-Key", **_PROBLEM},
    422: {"description": "Validation or business-rule error", **_PROBLEM},
}


async def _idempotent(
    request: Request,
    session: SessionDep,
    key: str,
    body: BaseModel | None,
    work: Callable[[], Awaitable[WorkResult]],
    **path_params: int,
) -> JSONResponse:
    payload = body.model_dump(mode="json") if body is not None else {}
    # Route template filled with *parsed* path params, so /entries/007 == /entries/7.
    path = request.scope["route"].path.format(**path_params)
    fingerprint = request_fingerprint(request.method, path, payload)
    outcome = await run_idempotent(session, key, fingerprint, work)
    return JSONResponse(
        outcome.body,
        status_code=outcome.status_code,
        headers={REPLAY_HEADER: "true" if outcome.replayed else "false"},
    )


def _entry_result(view: ledger.EntryView) -> WorkResult:
    return WorkResult(201, EntryOut.of(view).model_dump(mode="json"), entry_id=view.id)


@router.post("/accounts", status_code=201, response_model=AccountOut, responses=_COMMON_ERRORS)
async def open_account(
    request: Request, session: SessionDep, key: IdempotencyKeyDep, body: OpenAccountRequest
) -> JSONResponse:
    async def work() -> WorkResult:
        view = await ledger.open_account(session, body.currency)
        return WorkResult(201, AccountOut.of(view).model_dump(mode="json"))

    return await _idempotent(request, session, key, body, work)


@router.post("/deposits", status_code=201, response_model=EntryOut, responses=_COMMON_ERRORS)
async def deposit(
    request: Request, session: SessionDep, key: IdempotencyKeyDep, body: DepositRequest
) -> JSONResponse:
    async def work() -> WorkResult:
        amount = Money(body.amount_minor, body.currency)
        return _entry_result(
            await ledger.deposit(session, body.account_id, amount, body.description)
        )

    return await _idempotent(request, session, key, body, work)


@router.post("/withdrawals", status_code=201, response_model=EntryOut, responses=_COMMON_ERRORS)
async def withdraw(
    request: Request, session: SessionDep, key: IdempotencyKeyDep, body: WithdrawalRequest
) -> JSONResponse:
    async def work() -> WorkResult:
        amount = Money(body.amount_minor, body.currency)
        return _entry_result(
            await ledger.withdraw(session, body.account_id, amount, body.description)
        )

    return await _idempotent(request, session, key, body, work)


@router.post("/transfers", status_code=201, response_model=EntryOut, responses=_COMMON_ERRORS)
async def transfer(
    request: Request, session: SessionDep, key: IdempotencyKeyDep, body: TransferRequest
) -> JSONResponse:
    async def work() -> WorkResult:
        amount = Money(body.amount_minor, body.currency)
        view = await ledger.transfer(
            session, body.from_account_id, body.to_account_id, amount, body.description
        )
        return _entry_result(view)

    return await _idempotent(request, session, key, body, work)


@router.post(
    "/entries/{entry_id}/reverse",
    status_code=201,
    response_model=EntryOut,
    responses=_COMMON_ERRORS,
)
async def reverse_entry(
    request: Request,
    session: SessionDep,
    key: IdempotencyKeyDep,
    entry_id: PathId,
    body: ReverseRequest | None = None,
) -> JSONResponse:
    async def work() -> WorkResult:
        description = body.description if body is not None else None
        return _entry_result(await ledger.reverse_entry(session, entry_id, description))

    # An absent body and ``{}`` fingerprint identically.
    return await _idempotent(
        request, session, key, body or ReverseRequest(), work, entry_id=entry_id
    )


@router.get("/accounts/{account_id}/balance", response_model=BalanceOut)
async def get_balance(session: SessionDep, account_id: PathId) -> BalanceOut:
    return BalanceOut.of(await ledger.get_balance(session, account_id))


@router.get("/accounts/{account_id}/entries", response_model=EntryPage)
async def list_entries(
    session: SessionDep,
    account_id: PathId,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    before_id: Annotated[int | None, Query(gt=0, le=BIGINT_MAX)] = None,
) -> EntryPage:
    views = await ledger.list_entries(session, account_id, limit=limit, before_id=before_id)
    return EntryPage(
        account_id=account_id,
        entries=[EntryOut.of(v) for v in views],
        next_before_id=views[-1].id if len(views) == limit else None,
    )
