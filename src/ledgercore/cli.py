"""``ledgercore-drift-check``: compare the balance projection against SUM(entry_lines).

Prints a JSON report on stdout. Exit code 0 means no drift was found, 1 means drift was
found (even when ``--repair`` fixed it, so schedulers still get alerted), 2 means the check
itself failed (e.g. the database is unreachable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from ledgercore.db.posting import Drift, find_drift, rebuild_balances
from ledgercore.db.session import create_engine, create_sessionmaker


def _report(drifts: Sequence[Drift], repaired: bool) -> str:
    return json.dumps(
        {
            "drift_count": len(drifts),
            "repaired": repaired,
            "accounts": [
                {
                    "account_id": d.account_id,
                    "projected_minor": d.projected_minor,
                    "computed_minor": d.computed_minor,
                }
                for d in drifts
            ],
        },
        indent=2,
    )


async def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledgercore-drift-check", description=(__doc__ or "").split("\n\n")[0]
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="rebuild drifting balance rows from entry_lines (exit code still 1 if drift found)",
    )
    args = parser.parse_args(argv)

    engine = create_engine()
    try:
        sm = create_sessionmaker(engine)
        async with sm() as session, session.begin():
            drifts = await (rebuild_balances(session) if args.repair else find_drift(session))
    finally:
        await engine.dispose()
    print(_report(drifts, repaired=args.repair and bool(drifts)))
    return 1 if drifts else 0


def main() -> None:
    try:
        code = asyncio.run(run())
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
