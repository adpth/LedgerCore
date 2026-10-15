# ADR 0006: Append-only ledger via triggers, plus a restricted DB role (planned)

Status: Accepted (triggers implemented; restricted role not yet implemented)

## Context
Ledger history must be immutable. Corrections are made with reversal entries, never edits.

## Decision
1. Implemented: `BEFORE UPDATE OR DELETE` row triggers and `BEFORE TRUNCATE` statement
   triggers on `entry_lines` and `journal_entries` raise `restrict_violation`
   (`ledger_reject_mutation()`). Reversals are new entries linked by a unique
   `reverses_entry_id`, so an entry can be reversed at most once.
2. Planned, not in the migrations today: run the application as a role that has only
   `SELECT, INSERT` on the ledger tables and `UPDATE` on `balances` and
   `idempotency_keys`, with no ownership, no `TRIGGER` privilege and no superuser. The
   local compose and test setups connect as the database owner/superuser.

## Consequences
- Accidental or buggy UPDATE/DELETE/TRUNCATE on history fails loudly.
- A superuser or table owner can still disable the triggers (for instance with
  `session_replication_role = replica`; the test suite does this for cleanup). Triggers
  alone are therefore not a defense against a malicious or over-privileged DB user, which
  is why the restricted role is part of the decision.
- Until the role exists, immutability against a privileged operator relies on process and
  access control, not on the database.
