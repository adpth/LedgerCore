"""``ledgercore-payout-recovery``: resume payouts a crash (or a slow provider) left half-done.

Finds payouts whose ``updated_at`` is older than ``--stale-after-s`` and that are PENDING,
HELD, PROVIDER_PENDING or FAILED (release owed), plus NEEDS_RECONCILIATION (re-asked with
``get_status`` only, never re-submitted), and calls ``PayoutSaga.advance(recovering=True)``.
Details and the safety argument: docs/adr/0008-payout-saga.md.

Concurrency: several workers may run. DB-only steps lock the payout row with
``FOR UPDATE SKIP LOCKED`` so a row another worker is mid-transaction on is skipped, not
waited for. Provider calls happen with no lock held (by design), so two workers can both
call the provider for the same payout; that is safe because the provider is idempotent by
payout id, and the result is applied under a blocking row lock that re-checks the status,
so the ledger entry is posted once.

The only provider available in this repo is ``FakePayoutProvider``; running this console
script against it is for local demos. A real deployment must construct its own provider.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgercore.config import get_settings
from ledgercore.db.session import create_engine, create_sessionmaker
from ledgercore.domain import FakePayoutProvider, PayoutProvider, PayoutStatus
from ledgercore.services.payouts import PayoutSaga

log = logging.getLogger("ledgercore.payout_recovery")

STUCK = (
    PayoutStatus.PENDING,
    PayoutStatus.HELD,
    PayoutStatus.PROVIDER_PENDING,
    PayoutStatus.FAILED,
    PayoutStatus.NEEDS_RECONCILIATION,
)


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    examined: int
    errors: int


_STUCK_SQL = text(
    """
    SELECT id FROM payouts
    WHERE status = ANY(:statuses) AND updated_at < now() - make_interval(secs => :stale)
    ORDER BY updated_at LIMIT :n
    """
)


async def find_stuck(
    sm: async_sessionmaker[AsyncSession], stale_after_s: float, limit: int
) -> list[str]:
    async with sm() as s, s.begin():
        rows = await s.execute(
            _STUCK_SQL,
            {"statuses": [st.value for st in STUCK], "stale": stale_after_s, "n": limit},
        )
        return [r[0] for r in rows]


async def run_once(
    sm: async_sessionmaker[AsyncSession],
    provider: PayoutProvider,
    stale_after_s: float = 60.0,
    limit: int = 50,
    saga: PayoutSaga | None = None,
) -> RecoveryReport:
    saga = saga or PayoutSaga(sm, provider)
    ids = await find_stuck(sm, stale_after_s, limit)
    errors = 0
    for payout_id in ids:
        try:
            await saga.advance(payout_id, recovering=True)
        except Exception:
            errors += 1
            log.exception("recovery of payout %s failed; will retry next pass", payout_id)
    return RecoveryReport(len(ids), errors)


async def _serve(stale_after_s: float, interval_s: float, once: bool) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.warning("using FakePayoutProvider: no real provider is wired into this repo")
    engine = create_engine(get_settings())
    sm = create_sessionmaker(engine)
    provider = FakePayoutProvider()
    code = 0
    try:
        while not stop.is_set():
            report = await run_once(sm, provider, stale_after_s)
            log.info("recovery pass: examined=%d errors=%d", report.examined, report.errors)
            if report.errors:
                code = 1
            if once:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval_s)
    finally:
        await engine.dispose()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="ledgercore-payout-recovery", description=__doc__)
    parser.add_argument("--stale-after-s", type=float, default=settings.payout_stale_after_s)
    parser.add_argument("--interval-s", type=float, default=settings.payout_recovery_interval_s)
    parser.add_argument("--once", action="store_true", help="one pass, then exit (1 if any error)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return asyncio.run(_serve(args.stale_after_s, args.interval_s, args.once))
    except Exception:
        log.exception("payout recovery failed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
