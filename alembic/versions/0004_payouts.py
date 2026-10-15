"""external payouts: system account kinds + payouts table with a transition guard

* ``accounts.system_kind`` (external | holds | clearing): a currency now needs several
  system accounts, so uniqueness moves from (currency) to (currency, system_kind) among
  system rows. Existing system rows were all the settlement account, so they become
  ``external``. CHECK ``is_system = (system_kind IS NOT NULL)`` keeps the two in step.
* ``payouts``: one row per payout saga (docs/adr/0008-payout-saga.md). ``id`` is the
  provider idempotency key, so it is text and never changes.
* Guard trigger (a plain BEFORE UPDATE/DELETE row trigger, kept small on purpose):
  - DELETE is rejected;
  - a terminal row (COMPLETED, RELEASED, REJECTED) cannot be updated at all;
  - identity columns cannot change, and the three entry links cannot change once set;
  - a status change must follow the legal transition table (mirrors domain.payout).
  The table is duplicated in SQL and in Python on purpose (ADR 0002); a unit test pins them.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UP = [
    "ALTER TABLE accounts ADD COLUMN system_kind TEXT",
    "UPDATE accounts SET system_kind = 'external' WHERE is_system",
    """
    ALTER TABLE accounts
        ADD CONSTRAINT ck_accounts_system_kind_values
            CHECK (system_kind IN ('external', 'holds', 'clearing')),
        ADD CONSTRAINT ck_accounts_system_kind_matches_flag
            CHECK (is_system = (system_kind IS NOT NULL))
    """,
    "DROP INDEX uq_accounts_system_currency",
    """
    CREATE UNIQUE INDEX uq_accounts_system_currency_kind
        ON accounts (currency, system_kind) WHERE is_system
    """,
    """
    CREATE TABLE payouts (
        id TEXT PRIMARY KEY,
        account_id BIGINT NOT NULL,
        amount_minor BIGINT NOT NULL CONSTRAINT ck_payouts_amount_positive CHECK (amount_minor > 0),
        currency CHAR(3) NOT NULL,
        destination TEXT NOT NULL CONSTRAINT ck_payouts_destination CHECK (destination <> ''),
        status TEXT NOT NULL CONSTRAINT ck_payouts_status CHECK (status IN (
            'PENDING', 'HELD', 'PROVIDER_PENDING', 'COMPLETED', 'FAILED', 'RELEASED',
            'REJECTED', 'NEEDS_RECONCILIATION')),
        hold_entry_id BIGINT REFERENCES journal_entries(id),
        final_entry_id BIGINT REFERENCES journal_entries(id),
        release_entry_id BIGINT REFERENCES journal_entries(id),
        provider_reference TEXT,
        failure_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        version BIGINT NOT NULL DEFAULT 0,
        CONSTRAINT fk_payouts_account_currency
            FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency)
    )
    """,
    "CREATE INDEX ix_payouts_open ON payouts (updated_at) WHERE status NOT IN ('COMPLETED', 'RELEASED', 'REJECTED')",
    """
    CREATE FUNCTION payouts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'DELETE on payouts is not allowed' USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.status IN ('COMPLETED', 'RELEASED', 'REJECTED') THEN
            RAISE EXCEPTION 'payout % is terminal (%) and cannot change', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.account_id IS DISTINCT FROM OLD.account_id
           OR NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.destination IS DISTINCT FROM OLD.destination
           OR (OLD.hold_entry_id IS NOT NULL AND NEW.hold_entry_id IS DISTINCT FROM OLD.hold_entry_id)
           OR (OLD.final_entry_id IS NOT NULL AND NEW.final_entry_id IS DISTINCT FROM OLD.final_entry_id)
           OR (OLD.release_entry_id IS NOT NULL AND NEW.release_entry_id IS DISTINCT FROM OLD.release_entry_id)
        THEN
            RAISE EXCEPTION 'payout % identity or entry links cannot be changed', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.status <> OLD.status AND NOT (
               (OLD.status = 'PENDING' AND NEW.status IN ('HELD', 'REJECTED'))
            OR (OLD.status = 'HELD' AND NEW.status = 'PROVIDER_PENDING')
            OR (OLD.status = 'PROVIDER_PENDING' AND NEW.status IN ('COMPLETED', 'FAILED', 'NEEDS_RECONCILIATION'))
            OR (OLD.status = 'NEEDS_RECONCILIATION' AND NEW.status IN ('COMPLETED', 'FAILED'))
            OR (OLD.status = 'FAILED' AND NEW.status = 'RELEASED'))
        THEN
            RAISE EXCEPTION 'illegal payout transition % -> % for %', OLD.status, NEW.status, OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER trg_payouts_guard BEFORE UPDATE OR DELETE ON payouts
        FOR EACH ROW EXECUTE FUNCTION payouts_guard()
    """,
    """
    CREATE TRIGGER trg_payouts_no_truncate BEFORE TRUNCATE ON payouts
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation()
    """,
]

DOWN = [
    "DROP TABLE payouts",
    "DROP FUNCTION payouts_guard()",
    "DROP INDEX uq_accounts_system_currency_kind",
    # Only the external kind can be represented by the old per-currency unique index. Holds /
    # clearing accounts may hold money, so refuse to silently merge or drop them.
    """
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM accounts WHERE is_system AND system_kind <> 'external') THEN
            RAISE EXCEPTION 'cannot downgrade: holds/clearing system accounts exist';
        END IF;
    END $$
    """,
    "ALTER TABLE accounts DROP CONSTRAINT ck_accounts_system_kind_matches_flag",
    "ALTER TABLE accounts DROP CONSTRAINT ck_accounts_system_kind_values",
    "ALTER TABLE accounts DROP COLUMN system_kind",
    "CREATE UNIQUE INDEX uq_accounts_system_currency ON accounts (currency) WHERE is_system",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
