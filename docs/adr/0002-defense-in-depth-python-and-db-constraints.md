# ADR 0002: Defense in depth, Python validation plus deferred constraint triggers

Status: Accepted

## Context
The core invariant (every entry sums to zero per currency, at least two lines) must hold
even if application code has a bug or someone writes SQL directly.

## Decision
Enforce invariants in two independent layers.
1. Python: `domain.Entry` constructors and `_validate_shape` in `db/posting.py` reject
   fewer than 2 lines, zero amounts, bad currency codes and unbalanced sums before any SQL.
2. Postgres: `DEFERRABLE INITIALLY DEFERRED` constraint triggers on `entry_lines` and
   `journal_entries` run `ledger_check_entry_balanced()` at COMMIT, so lines can be
   inserted one at a time. The header trigger covers an entry with zero lines, which the
   line trigger would never see. A `BEFORE INSERT OR UPDATE` trigger on `balances`
   rejects negative balances unless `accounts.allow_negative`. Currency matching is a
   declarative composite FK, not a trigger.

## Consequences
- Raw SQL that bypasses the application is still rejected at commit (covered by tests).
- Python errors are friendly and early; DB errors are the last line of defense and are
  less descriptive.
- Constraint triggers add per-line work at commit.
- `allow_negative` changes are not revalidated against existing balances.
- A superuser can disable triggers (e.g. `session_replication_role = replica`); see ADR 0006.
