-- Run as the schema owner after the initial migration. Provision the login separately.
GRANT USAGE ON SCHEMA public TO ledger_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ledger_runtime;
GRANT SELECT, INSERT ON accounts, balances, entries, lines, idempotency, outbox, payouts
    TO ledger_runtime;
GRANT UPDATE (amount) ON balances TO ledger_runtime;
GRANT UPDATE (status, response) ON idempotency TO ledger_runtime;
GRANT UPDATE (sent_at) ON outbox TO ledger_runtime;
GRANT UPDATE (status, final_entry, retry_at, provider_status) ON payouts TO ledger_runtime;
-- Row locks require an UPDATE grant. The journal trigger rejects actual updates.
GRANT UPDATE (writer) ON entries TO ledger_runtime;
GRANT UPDATE (id) ON accounts TO ledger_runtime;
