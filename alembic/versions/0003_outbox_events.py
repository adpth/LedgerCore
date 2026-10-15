"""transactional outbox: outbox_events table, append-only-ish guard

``post_entry`` (and any other writer) inserts an event row in the same transaction as the
state change; ``ledgercore-outbox-relay`` later publishes it to Kafka and sets
``published_at``. See docs/adr/0007-transactional-outbox.md.

* ``seq`` (identity) is the relay's ordering key; ``event_id`` (UUID) is the consumer
  dedupe key.
* The partial index keeps "what is left to publish" cheap no matter how many published
  rows accumulate.
* Only ``published_at`` may change after insert; DELETE and TRUNCATE are rejected, with the
  same caveat as the ledger tables (a superuser / owner can disable triggers).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UP = [
    """
    CREATE TABLE outbox_events (
        seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        event_id UUID NOT NULL CONSTRAINT uq_outbox_events_event_id UNIQUE,
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX ix_outbox_events_unpublished ON outbox_events (seq) WHERE published_at IS NULL",
    """
    CREATE FUNCTION outbox_reject_non_publish_update() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF (to_jsonb(NEW) - 'published_at') IS DISTINCT FROM (to_jsonb(OLD) - 'published_at') THEN
            RAISE EXCEPTION 'UPDATE on outbox_events may only change published_at'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER trg_outbox_events_update_guard BEFORE UPDATE ON outbox_events
        FOR EACH ROW EXECUTE FUNCTION outbox_reject_non_publish_update()
    """,
    """
    CREATE TRIGGER trg_outbox_events_no_delete BEFORE DELETE ON outbox_events
        FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()
    """,
    """
    CREATE TRIGGER trg_outbox_events_no_truncate BEFORE TRUNCATE ON outbox_events
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation()
    """,
]

DOWN = [
    "DROP TABLE outbox_events",
    "DROP FUNCTION outbox_reject_non_publish_update()",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
