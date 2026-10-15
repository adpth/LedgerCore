"""system accounts: one external/settlement account per currency

Deposits and withdrawals post against a per-currency "external" account that represents
money outside the ledger. It is created lazily by the service layer with
``INSERT ... ON CONFLICT DO NOTHING`` against the partial unique index below, so two
requests racing to create the USD system account cannot create two of them.

* ``is_system`` is a plain flag on accounts (no new table, so existing cleanup / FKs are
  unaffected).
* A system account must allow negative balances (it is the mirror image of all customer
  money that has entered the ledger), enforced by CHECK.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP = [
    "ALTER TABLE accounts ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT false",
    """
    ALTER TABLE accounts ADD CONSTRAINT ck_accounts_system_allows_negative
        CHECK (NOT is_system OR allow_negative)
    """,
    "CREATE UNIQUE INDEX uq_accounts_system_currency ON accounts (currency) WHERE is_system",
]

DOWN = [
    "DROP INDEX uq_accounts_system_currency",
    "ALTER TABLE accounts DROP CONSTRAINT ck_accounts_system_allows_negative",
    "ALTER TABLE accounts DROP COLUMN is_system",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
