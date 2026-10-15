# ADR 0007: Transactional outbox with a single-leader Kafka relay

Status: Accepted

## Context
Other systems need to learn about ledger changes. Publishing to Kafka directly from the
request path cannot be atomic with the Postgres commit: either the entry commits and the
publish is lost, or the publish happens and the transaction rolls back.

## Decision
1. **Outbox table.** `post_entry` inserts an `entry.posted` row into `outbox_events` in the
   caller's transaction (`write_outbox_event`, reusable by other writers). The event exists
   if and only if the entry commits. Idempotent replays return the stored response without
   calling `post_entry`, so they add no event; a rejected double reversal rolls back and
   adds none. Payload: entry id, type, `reverses_entry_id`, lines (account, signed
   amount, currency), `created_at`. `event_id` is a Python `uuid4` (unique constraint);
   `uuid.uuid7` does not exist on Python 3.13.
2. **Guarded table.** Migration 0003 adds triggers: DELETE and TRUNCATE are rejected, and
   an UPDATE is rejected unless only `published_at` changed. A partial index on
   `seq WHERE published_at IS NULL` serves the relay's poll.
3. **Relay** (`ledgercore-outbox-relay`, `make relay`). Polls unpublished rows in `seq`
   order, sends each to Kafka (key `aggregate_id`, headers `event_id` and `event_type`,
   idempotent producer, `acks=all`), awaits the ack, then sets `published_at` for the
   acked rows in one UPDATE. `--lag` prints count and age of the oldest unpublished row.
4. **Ordering design: one active relay via a session-level advisory lock.**
   `FOR UPDATE SKIP LOCKED` lets two relays take disjoint batches and publish them
   concurrently, so a later event of an aggregate can overtake an earlier one. With the
   advisory lock only one relay publishes; others idle and retry each poll, and take over
   when the leader's connection drops (crash, network loss), because Postgres releases the
   lock with the session. The relay sends sequentially and stops at the first failure so
   nothing later overtakes a failed row.

## Delivery semantics
At-least-once. If the relay crashes, or the DB write fails, after the broker acked but
before `published_at` is set, the row is published again with the same `event_id`.
Consumers must dedupe on the `event_id` header; `outbox/consumer.py` is a reference (its
in-memory store is a test aid; a real consumer must record `event_id` in the same
transaction as the effect).

## Ordering guarantee and its limits
- Delivered order equals `seq` order, and Kafka preserves order per key within a
  partition, so events of one aggregate arrive in `seq` order.
- `seq` is assigned at INSERT, not COMMIT. For one aggregate this is only meaningful if
  its writers are serialized (for `entry.posted` each entry is its own aggregate and has
  one event; later saga events must be written under a lock on the saga row).
  Across aggregates there is no ordering guarantee.
- Another transaction with a lower `seq` may commit after the relay has read a higher one;
  this is harmless for the guarantee above but means cross-aggregate order is not commit
  order.
- Re-publication after a failed mark puts a duplicate of an old event after newer events
  of the same aggregate; consumers that need strict order should also ignore duplicates by
  `event_id` (the reference helper does).
- Changing the topic's partition count remaps keys and breaks per-key order across the
  change.

## Known limitations
- Throughput is bounded by one relay sending one message at a time; not measured.
- Published rows are never deleted or archived (DELETE is forbidden); the table grows.
  The partial index keeps polling cheap, but retention needs a future decision.
- A failing poison row (for example an oversized message) blocks all later rows, by design.
- Leadership is checked with a ping on the lock connection each iteration; a network
  partition that leaves Postgres holding the old session briefly can delay failover.
  Even if two relays overlapped, the result is duplicates, not loss.
- The triggers can be bypassed by a superuser or owner, as with ADR 0006.
- No metrics or tracing yet (planned with OpenTelemetry); logging is stdlib `logging`.
