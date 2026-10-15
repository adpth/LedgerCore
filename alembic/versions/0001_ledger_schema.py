"""ledger schema: tables, double-entry constraint trigger, append-only, balance guard

Design notes
------------
* Currency match: composite FK entry_lines(account_id, currency) -> accounts(id, currency)
  (and the same for balances). Declarative, no trigger, checked on every insert; chosen
  over a trigger because the planner/constraint machinery does it for free and it cannot
  be disabled by accident. Cost: accounts needs UNIQUE(id, currency).
* Balance / entry balance: DEFERRABLE INITIALLY DEFERRED constraint triggers, evaluated at
  COMMIT so lines may be inserted one by one inside a transaction.
* Append-only: BEFORE UPDATE/DELETE row triggers and BEFORE TRUNCATE statement triggers.
  (A superuser can still disable triggers, e.g. session_replication_role=replica.)
* balances >= 0 unless accounts.allow_negative: trigger (CHECK cannot read another table).
  Changing allow_negative later is not re-validated against existing balances.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UP = [
    """
    CREATE TABLE accounts (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        currency CHAR(3) NOT NULL CONSTRAINT ck_accounts_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
        allow_negative BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_accounts_id_currency UNIQUE (id, currency)
    )
    """,
    """
    CREATE TABLE journal_entries (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        entry_type TEXT NOT NULL,
        reverses_entry_id BIGINT UNIQUE REFERENCES journal_entries(id),
        description TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE entry_lines (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        entry_id BIGINT NOT NULL REFERENCES journal_entries(id),
        account_id BIGINT NOT NULL,
        amount_minor BIGINT NOT NULL CONSTRAINT ck_entry_lines_nonzero CHECK (amount_minor <> 0),
        currency CHAR(3) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT fk_entry_lines_account_currency
            FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency)
    )
    """,
    "CREATE INDEX ix_entry_lines_entry_id ON entry_lines (entry_id)",
    "CREATE INDEX ix_entry_lines_account_id ON entry_lines (account_id)",
    """
    CREATE TABLE balances (
        account_id BIGINT PRIMARY KEY,
        currency CHAR(3) NOT NULL,
        balance_minor BIGINT NOT NULL DEFAULT 0,
        version BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT fk_balances_account_currency
            FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency)
    )
    """,
    """
    CREATE TABLE idempotency_keys (
        key TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
            CONSTRAINT ck_idempotency_status CHECK (status IN ('PENDING', 'COMPLETED')),
        response_status INTEGER,
        response_body JSONB,
        entry_id BIGINT REFERENCES journal_entries(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ
    )
    """,
    # --- every account gets a zero balance row ---
    """
    CREATE FUNCTION ledger_create_balance_row() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        INSERT INTO balances (account_id, currency) VALUES (NEW.id, NEW.currency);
        RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_accounts_create_balance AFTER INSERT ON accounts
        FOR EACH ROW EXECUTE FUNCTION ledger_create_balance_row()
    """,
    # --- double-entry invariant, checked at COMMIT ---
    """
    CREATE FUNCTION ledger_check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        v_entry_id BIGINT;
        v_count INTEGER;
        v_currency TEXT;
        v_sum NUMERIC;
    BEGIN
        IF TG_TABLE_NAME = 'journal_entries' THEN
            v_entry_id := NEW.id;
        ELSE
            v_entry_id := NEW.entry_id;
        END IF;
        SELECT count(*) INTO v_count FROM entry_lines WHERE entry_id = v_entry_id;
        IF v_count < 2 THEN
            RAISE EXCEPTION 'journal entry % must have at least 2 lines (has %)', v_entry_id, v_count
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT currency, sum(amount_minor) INTO v_currency, v_sum
          FROM entry_lines WHERE entry_id = v_entry_id
          GROUP BY currency HAVING sum(amount_minor) <> 0 LIMIT 1;
        IF FOUND THEN
            RAISE EXCEPTION 'journal entry % is unbalanced in currency % (sum %)', v_entry_id, v_currency, v_sum
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END $$
    """,
    """
    CREATE CONSTRAINT TRIGGER trg_entry_lines_balanced AFTER INSERT ON entry_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_balanced()
    """,
    # An entry with zero lines never fires the line trigger, so check on the header too.
    """
    CREATE CONSTRAINT TRIGGER trg_journal_entries_balanced AFTER INSERT ON journal_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_balanced()
    """,
    # --- append-only ---
    """
    CREATE FUNCTION ledger_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% on % is not allowed: ledger tables are append-only', TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END $$
    """,
    *[
        stmt
        for t in ("entry_lines", "journal_entries")
        for stmt in (
            f"""CREATE TRIGGER trg_{t}_no_update_delete BEFORE UPDATE OR DELETE ON {t}
                FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()""",
            f"""CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t}
                FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation()""",
        )
    ],
    # --- last line of defence: non-negative balances ---
    """
    CREATE FUNCTION ledger_check_balance_nonnegative() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.balance_minor < 0 AND NOT (
            SELECT allow_negative FROM accounts WHERE id = NEW.account_id
        ) THEN
            RAISE EXCEPTION 'balance of account % would be negative (%)', NEW.account_id, NEW.balance_minor
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER trg_balances_nonnegative BEFORE INSERT OR UPDATE ON balances
        FOR EACH ROW EXECUTE FUNCTION ledger_check_balance_nonnegative()
    """,
]

DOWN = [
    "DROP TABLE idempotency_keys",
    "DROP TABLE balances",
    "DROP TABLE entry_lines",
    "DROP TABLE journal_entries",
    "DROP TABLE accounts",
    "DROP FUNCTION ledger_create_balance_row()",
    "DROP FUNCTION ledger_check_entry_balanced()",
    "DROP FUNCTION ledger_reject_mutation()",
    "DROP FUNCTION ledger_check_balance_nonnegative()",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
