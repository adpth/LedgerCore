# LedgerCore

A double-entry ledger service: FastAPI, async SQLAlchemy 2, PostgreSQL 16, Python 3.13.

## What and why

LedgerCore records money movements as immutable, balanced journal entries and keeps a
fast balance projection. The goal is a small service where the money invariants are
enforced by the database itself, not just by application code, and where retries are safe.

Guarantees (see `docs/adr/` for the reasoning and trade-offs):

- Every entry sums to zero per currency and has at least 2 lines, checked in Python and by
  a deferred constraint trigger at COMMIT.
- Amounts are signed integer minor units (debit positive, credit negative); no floats.
- Concurrent postings serialize via row locks taken in ascending account-id order, so no
  lost updates, no overdraws, no deadlocks from opposing transfers.
- Ledger history is append-only (triggers reject UPDATE/DELETE/TRUNCATE); mistakes are
  fixed with reversal entries.
- Mutating requests require an `Idempotency-Key`; retries replay the original response.
- Every posted entry writes an `entry.posted` outbox row in the same transaction; a relay
  publishes it to Kafka at least once (consumers dedupe on `event_id`; ADR 0007).
- External payouts run as a saga (hold, call provider, settle or release); an unknown
  provider outcome never releases funds (ADR 0008).
- The balance projection can be audited and rebuilt from entry lines (`make drift-check`).

## Architecture

```
HTTP (FastAPI routes, RFC 7807 problem+json errors)
  -> api/        request schemas, Idempotency-Key dependency, one transaction per request
  -> services/   ledger use cases (deposit, withdraw, transfer, reverse), idempotency protocol,
                 payout saga + recovery worker
  -> domain/     Money, Entry constructors, pure validation (no I/O)
  -> db/         post_entry (locking + projection update), find_drift / rebuild_balances
PostgreSQL: accounts, journal_entries, entry_lines, balances, idempotency_keys,
            outbox_events, payouts + triggers
  -> outbox/     relay: outbox_events -> Kafka topic (at-least-once), reference consumer
```

Deposits and withdrawals post against a lazily created per-currency system (external)
account; payouts also use per-currency `holds` and `clearing` system accounts. Clients
cannot name system accounts directly.

## Quickstart

```sh
cp .env.example .env
make sync      # install deps (uv)
make up        # start Postgres via docker compose
make migrate   # alembic upgrade head
make api       # http://127.0.0.1:8000  (docs at /docs, health at /healthz)
```

Create two USD accounts, fund one, transfer, then replay the transfer:

```sh
curl -s -X POST localhost:8000/accounts -H 'Idempotency-Key: open-1' \
  -H 'Content-Type: application/json' -d '{"currency":"USD"}'
curl -s -X POST localhost:8000/accounts -H 'Idempotency-Key: open-2' \
  -H 'Content-Type: application/json' -d '{"currency":"USD"}'

curl -s -X POST localhost:8000/deposits -H 'Idempotency-Key: dep-1' \
  -H 'Content-Type: application/json' \
  -d '{"account_id":1,"amount_minor":10000,"currency":"USD"}'

curl -si -X POST localhost:8000/transfers -H 'Idempotency-Key: xfer-1' \
  -H 'Content-Type: application/json' \
  -d '{"from_account_id":1,"to_account_id":2,"amount_minor":2500,"currency":"USD"}'

# Same key, same body: same status and JSON body (key order may differ), header
# "Idempotent-Replayed: true", no second transfer.
curl -si -X POST localhost:8000/transfers -H 'Idempotency-Key: xfer-1' \
  -H 'Content-Type: application/json' \
  -d '{"from_account_id":1,"to_account_id":2,"amount_minor":2500,"currency":"USD"}'

curl -s localhost:8000/accounts/1/balance
```

(Account ids shown assume a fresh database; the system account takes an id too, so use the
ids returned by your calls.)

Reusing a key with a different body returns 422 `idempotency_key_reuse`.

## API

Every POST requires an `Idempotency-Key` header (1-255 printable characters). Errors are
`application/problem+json` with a stable `code` field.

| Method and path | Purpose | Success |
|---|---|---|
| `POST /accounts` | open an account `{currency}` | 201 |
| `POST /deposits` | `{account_id, amount_minor, currency, description?}` | 201 |
| `POST /withdrawals` | same body as deposits | 201 |
| `POST /transfers` | `{from_account_id, to_account_id, amount_minor, currency, description?}` | 201 |
| `POST /entries/{entry_id}/reverse` | reverse an entry, optional `{description}` | 201 |
| `POST /payouts` | `{account_id, amount_minor, currency, destination}`; 201 at an end state, 202 in flight (REJECTED on insufficient funds is a 201 payout) | 201/202 |
| `GET /payouts/{id}` | payout status and entry ids | 200 |
| `POST /payouts/{id}/resolve` | `{outcome: succeeded\|failed}` for NEEDS_RECONCILIATION; **unauthenticated until Phase 3**, no Idempotency-Key needed | 200 |
| `GET /accounts/{id}/balance` | current balance | 200 |
| `GET /accounts/{id}/entries?limit=&before_id=` | newest-first, keyset pagination | 200 |
| `GET /healthz` | liveness | 200 |

| Status | `code` | Meaning |
|---|---|---|
| 400 | `missing_idempotency_key`, `invalid_idempotency_key` | header absent or malformed |
| 404 | `unknown_account`, `unknown_entry`, `not_found` | no such resource |
| 409 | `entry_already_reversed` | entry already has a reversal |
| 409 | `idempotency_key_in_progress` | defensive; not expected under the current protocol |
| 405 | `method_not_allowed` | wrong HTTP method for the path |
| 422 | `insufficient_funds` | would make a non-negative account negative |
| 422 | `balance_overflow` | resulting balance would exceed the BIGINT range |
| 422 | `currency_mismatch`, `invalid_currency`, `invalid_amount` | bad currency or amount |
| 422 | `unbalanced_entry`, `invalid_entry`, `same_account_transfer` | invalid entry shape |
| 422 | `system_account_not_allowed` | client named a system account |
| 422 | `entry_not_reversible` | tried to reverse a HOLD, HOLD_RELEASE or PAYOUT entry |
| 404 | `unknown_payout` | no such payout |
| 409 | `payout_not_resolvable` | payout is not awaiting that resolution |
| 422 | `idempotency_key_reuse` | key reused with a different request |
| 422 | `validation_error` | request body or parameters invalid |
| 500 | `internal_error` | unexpected error |

## Drift check

```sh
make drift-check                          # JSON report; exit 0 = clean, 1 = drift
uv run ledgercore-drift-check --repair    # rebuild drifting balances; still exits 1 if drift was found
```

Exit code 2 means the check could not run (for example the database is unreachable).
It takes no locks; see ADR 0004.

## Outbox relay

```sh
make up                                   # Postgres and Apache Kafka (KRaft, single node)
make relay                                # run the relay (SIGTERM / Ctrl-C stops it cleanly)
uv run ledgercore-outbox-relay --lag      # {"unpublished": N, "oldest_age_s": ...}
```

Settings: `LEDGER_KAFKA_BOOTSTRAP_SERVERS` (default `localhost:9092`), `LEDGER_OUTBOX_TOPIC`
(default `ledgercore.events`). Messages are keyed by `aggregate_id` with `event_id` and
`event_type` headers. Delivery is at-least-once; only one relay publishes at a time (others
stand by). See ADR 0007.

## Payouts

`POST /payouts` reserves the funds with a hold entry, calls the provider outside any DB
transaction, then posts the final entry or releases the hold. An unknown provider outcome
parks the payout in `NEEDS_RECONCILIATION` with the funds still held; it is settled by the
recovery worker (when the provider can answer) or by an operator via `/resolve`. The API
defaults to a fake provider that always succeeds, so no real money moves locally.

```sh
make payout-recovery                      # loop; SIGTERM / Ctrl-C stops it
uv run ledgercore-payout-recovery --once  # one pass; --stale-after-s N to tune
```

Settings: `LEDGER_PAYOUT_STALE_AFTER_S` (60), `LEDGER_PAYOUT_RECOVERY_INTERVAL_S` (5). The
recovery CLI only has the fake provider; a real one must be wired in. See ADR 0008.

## Testing

```sh
make check                  # ruff + ruff format --check + mypy (strict) + pytest
uv run pytest tests/unit    # no Docker needed
```

Integration tests start a real Postgres 16 and (for the relay) Apache Kafka 3.9 with
Testcontainers, so Docker must be running.
They cover raw-SQL invariant violations, concurrent transfers, idempotency races, the API
and the drift CLI.

### macOS gotcha: hidden `.venv`

On macOS the `.venv` directory can get the `hidden` flag, after which Python skips its
`.pth` files and the editable install stops importing (`ModuleNotFoundError: ledgercore`).
Fix:

```sh
chflags -R nohidden .venv
```

## Roadmap

- Phase 1 (current): ledger core, idempotent HTTP API, drift tooling, ADRs.
- Phase 2 (outbox and payout saga done, no real provider adapter): Kafka transactional outbox and a saga for multi-step flows.
- Phase 3: OAuth2 and RBAC, plus a scheduled nightly drift job.
- Phase 4: Terraform, EKS, Helm, OpenTelemetry and Grafana.
- Load testing with k6 (not yet written).

## Not yet done

- Throughput and latency are unmeasured; there are no benchmark numbers.
- No `lock_timeout` or `statement_timeout`, so a stuck transaction can block others.
- The per-currency system account is a serialization point for all deposits and
  withdrawals in that currency.
- `idempotency_keys` rows are never cleaned up.
- `find_drift` takes no locks and may report transient drift under concurrent writes.
- Changing `allow_negative` is not revalidated against existing balances.
- Append-only triggers can be bypassed by a superuser or table owner, and the restricted
  application DB role from ADR 0006 does not exist yet; the app currently connects as the
  database owner.
- No authentication or authorization (including `/payouts/{id}/resolve`), no deployment
  manifests, no real payout provider; see ADR 0008 for payout limitations.
- Outbox: published rows are never pruned, one relay publishes at a time, and there are no
  relay metrics yet (ADR 0007).
