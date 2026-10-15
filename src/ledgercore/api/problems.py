"""RFC 7807 ``application/problem+json`` error responses with stable ``code``s."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ledgercore.db import errors as db_errors
from ledgercore.domain.errors import DomainError
from ledgercore.services.errors import (
    EntryNotReversible,
    IdempotencyKeyInProgress,
    IdempotencyKeyReuse,
    PayoutNotFound,
    PayoutNotResolvable,
    SystemAccountNotAllowed,
)

PROBLEM_JSON = "application/problem+json"
TYPE_PREFIX = "urn:ledgercore:problem:"

log = logging.getLogger(__name__)


class ApiProblem(Exception):
    """Raised by the API layer itself (e.g. header problems)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(detail)


def problem(
    status: int, code: str, title: str, detail: str, instance: str, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{TYPE_PREFIX}{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "code": code,
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


#: exception type -> (HTTP status, stable code, title). Most specific first.
_MAPPING: list[tuple[type[Exception], int, str, str]] = [
    (db_errors.InsufficientFunds, 422, "insufficient_funds", "Insufficient funds"),
    (db_errors.BalanceOverflow, 422, "balance_overflow", "Balance overflow"),
    (db_errors.UnknownAccount, 404, "unknown_account", "Unknown account"),
    (db_errors.UnknownEntry, 404, "unknown_entry", "Unknown journal entry"),
    (db_errors.EntryAlreadyReversed, 409, "entry_already_reversed", "Entry already reversed"),
    (db_errors.CurrencyMismatch, 422, "currency_mismatch", "Currency mismatch"),
    (db_errors.UnbalancedEntry, 422, "unbalanced_entry", "Unbalanced entry"),
    (IdempotencyKeyReuse, 422, "idempotency_key_reuse", "Idempotency key reused"),
    (IdempotencyKeyInProgress, 409, "idempotency_key_in_progress", "Request in progress"),
    (SystemAccountNotAllowed, 422, "system_account_not_allowed", "System account not allowed"),
    (EntryNotReversible, 422, "entry_not_reversible", "Entry cannot be reversed directly"),
    (PayoutNotFound, 404, "unknown_payout", "Unknown payout"),
    (PayoutNotResolvable, 409, "payout_not_resolvable", "Payout is not awaiting resolution"),
]


def _extra_fields(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, db_errors.InsufficientFunds | db_errors.BalanceOverflow):
        return {"account_id": exc.account_id}
    if isinstance(exc, db_errors.UnknownAccount):
        return {"account_ids": exc.account_ids}
    if isinstance(exc, db_errors.UnknownEntry | db_errors.EntryAlreadyReversed):
        return {"entry_id": exc.entry_id}
    if isinstance(exc, SystemAccountNotAllowed):
        return {"account_id": exc.account_id}
    if isinstance(exc, EntryNotReversible):
        return {"entry_id": exc.entry_id, "entry_type": exc.entry_type}
    if isinstance(exc, PayoutNotFound | PayoutNotResolvable):
        return {"payout_id": exc.payout_id}
    return {}


async def _mapped_handler(request: Request, exc: Exception) -> JSONResponse:
    for exc_type, status, code, title in _MAPPING:
        if isinstance(exc, exc_type):
            return problem(status, code, title, str(exc), request.url.path, **_extra_fields(exc))
    return await _unhandled_handler(request, exc)  # pragma: no cover - mapping is total


async def _domain_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    title = exc.code.replace("_", " ").capitalize()
    return problem(422, exc.code, title, str(exc), request.url.path)


async def _api_problem_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiProblem)
    title = exc.code.replace("_", " ").capitalize()
    return problem(exc.status, exc.code, title, exc.detail, request.url.path)


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return problem(
        422,
        "validation_error",
        "Request validation failed",
        "The request body, path or query parameters are invalid.",
        request.url.path,
        errors=jsonable_encoder(errors),
    )


async def _http_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return problem(exc.status_code, code, str(exc.detail), str(exc.detail), request.url.path)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", exc_info=exc)
    return problem(
        500, "internal_error", "Internal server error", "Unexpected error.", request.url.path
    )


def install_problem_handlers(app: FastAPI) -> None:
    for exc_type, *_ in _MAPPING:
        app.add_exception_handler(exc_type, _mapped_handler)
    app.add_exception_handler(DomainError, _domain_handler)
    app.add_exception_handler(ApiProblem, _api_problem_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    # Starlette routes a bare ``Exception`` handler to ServerErrorMiddleware: it sends this
    # 500 problem response and then re-raises for the server to log.
    app.add_exception_handler(Exception, _unhandled_handler)
