from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("""
    ALTER TABLE balances ADD CONSTRAINT finite_integer_balance
        CHECK (amount > '-Infinity'::numeric AND amount < 'Infinity'::numeric
               AND amount = trunc(amount));
    ALTER TABLE idempotency ADD CONSTRAINT valid_idempotency_state
        CHECK ((status = 'PENDING' AND response IS NULL)
               OR (status = 'COMPLETED' AND response IS NOT NULL
                   AND jsonb_typeof(response) = 'object'));
    ALTER TABLE payouts ADD COLUMN provider_status varchar(12);
    UPDATE payouts SET provider_status = CASE status
        WHEN 'COMPLETED' THEN 'succeeded' WHEN 'DECLINED' THEN 'declined' END;
    ALTER TABLE payouts ADD CONSTRAINT valid_payout_state CHECK (
        amount > 0 AND wallet <> reserve AND wallet <> clearing AND reserve <> clearing
        AND (provider_status IS NULL OR provider_status IN ('succeeded', 'declined'))
        AND ((status = 'PENDING' AND final_entry IS NULL)
            OR (status = 'COMPLETED' AND final_entry IS NOT NULL
                AND (provider_status IS NULL OR provider_status = 'succeeded'))
            OR (status = 'DECLINED' AND final_entry IS NOT NULL
                AND (provider_status IS NULL OR provider_status = 'declined')))
        AND final_entry IS DISTINCT FROM id
    );
    ALTER TABLE payouts ADD CONSTRAINT unique_payout_final_entry UNIQUE (final_entry);
    """)


def downgrade():
    op.execute("""
    ALTER TABLE payouts DROP CONSTRAINT unique_payout_final_entry,
        DROP CONSTRAINT valid_payout_state, DROP COLUMN provider_status;
    ALTER TABLE idempotency DROP CONSTRAINT valid_idempotency_state;
    ALTER TABLE balances DROP CONSTRAINT finite_integer_balance;
    """)
