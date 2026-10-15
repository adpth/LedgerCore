# ADR 0004: Balance projection table instead of summing lines on read

Status: Accepted

## Context
The balance of an account is `SUM(entry_lines.amount_minor)`. Computing that on every
read and on every non-negative check grows with account history.

## Decision
Keep a `balances` table (one row per account, created by an `AFTER INSERT` trigger on
`accounts`) with `balance_minor` and a `version` counter, updated inside the same
transaction as the lines in `post_entry`. `entry_lines` remains the source of truth; the
projection is derived and can be rebuilt.

`find_drift` compares projection to `SUM(entry_lines)` in one SQL query;
`rebuild_balances` rewrites drifting rows from the sums. Both are exposed through the
`ledgercore-drift-check` CLI (`make drift-check`, optional `--repair`).

## Consequences
- O(1) balance reads and checks; the locked balance row also serves as the concurrency
  guard (ADR 0003).
- Two representations can disagree if someone edits `balances` directly or a bug skips
  the update; drift detection exists for that reason.
- `find_drift` takes no locks, so running it while writes are in flight can report
  transient or false drift. Run it quiesced, or re-check findings before repairing.
- `--repair` trusts the lines. It does not fix a corrupted `entry_lines`.
- Drift detection is manual so far; the scheduled nightly job is on the roadmap.
