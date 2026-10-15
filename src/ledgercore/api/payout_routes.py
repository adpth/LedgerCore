"""Payout HTTP routes (docs/adr/0008-payout-saga.md).

``POST /payouts`` is three phases on purpose, because the provider must never be called
inside a DB transaction: (1) one transaction claims the Idempotency-Key, creates the payout
and posts the hold, and commits; (2) the saga runs its remaining short transactions and
calls the provider with no transaction open; (3) the current payout is returned. If the
process dies between 1 and 2 the committed payout is finished by ``ledgercore-payout-recovery``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse

from ledgercore.api.deps import IdempotencyKeyDep, SessionDep
from ledgercore.api.schemas import PayoutOut, PayoutRequest, ResolvePayoutRequest
from ledgercore.domain import Money, PayoutProvider, request_fingerprint
from ledgercore.services import payouts
from ledgercore.services.idempotency import WorkResult, run_idempotent
from ledgercore.services.payouts import PayoutSaga, PayoutView

router = APIRouter()

REPLAY_HEADER = "Idempotent-Replayed"
PayoutId = Annotated[str, Path(min_length=1, max_length=64)]
_PROBLEM: dict[str, Any] = {"content": {"application/problem+json": {}}}


def _provider(request: Request) -> PayoutProvider:
    provider: PayoutProvider = request.app.state.payout_provider
    return provider


ProviderDep = Annotated[PayoutProvider, Depends(_provider)]


def _status_code(view: PayoutView) -> int:
    """201 once the payout has reached an end state, 202 while it is still in flight."""
    return 201 if view.is_terminal else 202


def _response(view: PayoutView, replayed: bool = False) -> JSONResponse:
    return JSONResponse(
        PayoutOut.of(view).model_dump(mode="json"),
        status_code=_status_code(view),
        headers={REPLAY_HEADER: "true" if replayed else "false"},
    )


@router.post(
    "/payouts",
    status_code=202,
    response_model=PayoutOut,
    responses={
        201: {"description": "Payout reached an end state", "model": PayoutOut},
        400: {"description": "Missing/invalid Idempotency-Key", **_PROBLEM},
        422: {"description": "Validation or business-rule error", **_PROBLEM},
    },
)
async def create_payout(
    request: Request, key: IdempotencyKeyDep, body: PayoutRequest, provider: ProviderDep
) -> JSONResponse:
    sm = request.app.state.sessionmaker
    fingerprint = request_fingerprint("POST", "/payouts", body.model_dump(mode="json"))
    async with sm() as session, session.begin():

        async def work() -> WorkResult:
            view = await payouts.create_payout(
                session,
                body.account_id,
                Money(body.amount_minor, body.currency),
                body.destination,
            )
            return WorkResult(
                202, PayoutOut.of(view).model_dump(mode="json"), entry_id=view.hold_entry_id
            )

        outcome = await run_idempotent(session, key, fingerprint, work)
    # Phase 1 is committed. A replay only reports the current state: the original request
    # (or recovery) drives the saga, so twins never pile onto the provider.
    saga = PayoutSaga(sm, provider)
    payout_id = str(outcome.body["id"])
    view = await (saga.get(payout_id) if outcome.replayed else saga.advance(payout_id))
    return _response(view, outcome.replayed)


@router.get("/payouts/{payout_id}", response_model=PayoutOut)
async def get_payout(session: SessionDep, payout_id: PayoutId) -> PayoutOut:
    return PayoutOut.of(await payouts.get_payout(session, payout_id))


@router.post(
    "/payouts/{payout_id}/resolve",
    response_model=PayoutOut,
    responses={409: {"description": "Payout is not awaiting resolution", **_PROBLEM}},
)
async def resolve_payout(
    request: Request, payout_id: PayoutId, body: ResolvePayoutRequest, provider: ProviderDep
) -> PayoutOut:
    """Operator verdict for a NEEDS_RECONCILIATION payout.

    UNAUTHENTICATED until Phase 3 (auth/RBAC): anyone who can reach the API can call it.
    State-idempotent instead of key-based: repeating the same verdict is a no-op, the
    opposite verdict is 409 ``payout_not_resolvable``. ``Idempotency-Key`` is not required.
    """
    saga = PayoutSaga(request.app.state.sessionmaker, provider)
    return PayoutOut.of(await saga.resolve(payout_id, body.outcome))
