import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
from confluent_kafka import Producer
from opentelemetry import metrics, trace
from sqlalchemy import select

from ledger.db import Entry, Outbox, Payout, Session
from ledger.service import drift, post

logger = logging.getLogger("ledger.worker")
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
outbox_lag = meter.create_histogram("ledger.outbox.lag", unit="s")
drift_count = meter.create_gauge("ledger.reconciliation.mismatches")


def relay(producer: Producer, topic: str) -> bool:
    with tracer.start_as_current_span("ledger.outbox.publish"), Session.begin() as db:
        event = db.scalar(
            select(Outbox)
            .where(Outbox.sent_at.is_(None))
            .order_by(Outbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return False
        errors = []
        producer.produce(
            topic,
            key=event.payload["event_id"],
            value=json.dumps(event.payload),
            on_delivery=lambda error, _: errors.append(error) if error else None,
        )
        if producer.flush(8) or errors:
            raise RuntimeError(f"Kafka delivery failed: {errors}")
        event.sent_at = datetime.now(UTC)
        outbox_lag.record(max(0, (event.sent_at - event.created_at).total_seconds()))
        return True


def settle(client: httpx.Client) -> bool:
    with tracer.start_as_current_span("ledger.payout.settle"), Session.begin() as db:
        payout = db.scalar(
            select(Payout)
            .where(Payout.status == "PENDING", Payout.retry_at <= datetime.now(UTC))
            .order_by(Payout.retry_at, Payout.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if payout is None:
            return False
        payout_id = payout.id
        payout.retry_at = datetime.now(UTC) + timedelta(seconds=30)
        if payout.provider_status is None:
            currency = db.get_one(Entry, payout.id).currency
            try:
                response = client.post(
                    os.environ["PAYOUT_URL"],
                    headers={"Idempotency-Key": f"ledger-payout-{payout.id}"},
                    json={
                        "destination": payout.destination,
                        "amount_minor": payout.amount,
                        "currency": currency,
                    },
                )
                response.raise_for_status()
                status = response.json()["status"]
                if status not in {"succeeded", "declined"}:
                    raise ValueError("Provider result is not terminal")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, RecursionError):
                logger.warning("Payout %s still pending", payout.id)
                return True

            payout.provider_status = status

    # Commit the provider outcome before posting the closing entry.
    with tracer.start_as_current_span("ledger.payout.finalize"), Session.begin() as db:
        payout = db.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
        if payout is None or payout.status != "PENDING":
            return True
        status = payout.provider_status
        assert status in {"succeeded", "declined"}
        success = status == "succeeded"
        operation, destination, kind = (
            ("payout_post", payout.clearing, "clearing")
            if success
            else ("payout_release", payout.wallet, "wallet")
        )
        result = post(
            db,
            operation,
            payout.reserve,
            destination,
            payout.amount,
            "reserve",
            kind,
            f"Payout {payout.id}",
        )
        payout.status = "COMPLETED" if success else "DECLINED"
        payout.final_entry = int(result["id"])
        return True


def main():
    logging.basicConfig(level=logging.INFO)
    mode = sys.argv[1]
    if mode == "reconcile":
        with Session() as db:
            mismatches = drift(db)
        drift_count.set(len(mismatches))
        provider = metrics.get_meter_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
        if mismatches:
            logger.error("Balance drift: %s", mismatches)
            return 1
        logger.info("All account balances match the journal")
        return 0
    if mode == "relay":
        config = {
            "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP"],
            "enable.idempotence": True,
            "delivery.timeout.ms": 7000,
            "security.protocol": os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"),
        }
        if config["security.protocol"] == "SASL_SSL":
            config.update(
                {
                    "sasl.mechanism": "SCRAM-SHA-512",
                    "sasl.username": os.environ["KAFKA_USERNAME"],
                    "sasl.password": os.environ["KAFKA_PASSWORD"],
                }
            )
        producer = Producer(config)
        while True:
            try:
                if not relay(producer, os.getenv("KAFKA_TOPIC", "ledger.entries")):
                    time.sleep(0.5)
            except Exception:
                logger.exception("Outbox delivery will retry")
                time.sleep(2)
    if mode == "payout":
        token = os.getenv("PAYOUT_TOKEN")
        with httpx.Client(
            timeout=5,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        ) as client:
            while True:
                try:
                    if not settle(client):
                        time.sleep(1)
                except Exception:
                    logger.exception("Payout settlement will retry")
                    time.sleep(2)
    raise SystemExit("Use relay, payout, or reconcile")


if __name__ == "__main__":
    sys.exit(main())
