"""
worker-service — Kafka consumer that processes jobs from the `jobs` topic.

Processing contract:
  - Deserialises JSON from `jobs` topic
  - Runs the registered handler for job["type"]
  - On success: commits offset manually (enable.auto.commit=false)
  - On failure: retries up to MAX_RETRIES; after that publishes to `jobs-dlq` then commits
  - UUID-based deduplication via an in-memory set (survives restarts via Kafka offset)
  - Drains gracefully on SIGTERM: finishes the in-flight message, commits, exits

Prometheus metrics exposed on :9090/metrics:
  worker_jobs_processed_total{status="success|error|dlq"}
  worker_job_duration_seconds (histogram)
  worker_consumer_lag (gauge, polled from assignment watermarks)

This module is the thin orchestration layer: per-job processing and the consume
loop. Supporting concerns live in the `app` package (config, observability,
metrics, kafka_io, handlers). Names re-exported below preserve the public
surface that the test suite and operators rely on.
"""

import json
import signal
import threading
import time
from collections import OrderedDict

from app.config import BOOTSTRAP_SERVERS, CONSUMER_GROUP, MAX_RETRIES, TOPIC_DLQ, TOPIC_JOBS
from app.handlers import _HANDLERS, handle_data_transform, handle_ping, register  # noqa: F401
from app.kafka_io import (
    kafka_header_getter,  # noqa: F401  (re-exported for completeness/tests)
    kafka_header_setter,
    make_consumer,
    make_producer,
    produce_confirmed,
    update_lag,
)
from app.metrics import CONSUMER_LAG, JOB_DURATION, JOBS_PROCESSED  # noqa: F401
from app.metrics_server import start_metrics_server
from app.observability import log, tracer
from confluent_kafka import KafkaError, KafkaException
from opentelemetry import propagate


def process_message(
    raw_value: bytes,
    producer,
    seen_ids: OrderedDict,
    headers: list[tuple[str, bytes]] | None = None,
) -> None:
    """Deserialise → deduplicate → dispatch → DLQ on repeated failure."""
    context = propagate.extract(headers or [], getter=kafka_header_getter)
    try:
        job = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        log.error("bad_json", error=str(exc), raw=raw_value[:200])
        JOBS_PROCESSED.labels(status="error").inc()
        return  # malformed message: commit and move on

    job_id = job.get("id", "")
    job_type = job.get("type", "unknown")

    # Deduplication — in-memory (within a pod lifetime).
    # seen_ids is an OrderedDict to keep the last 10,000 IDs (contract M14).
    if job_id and job_id in seen_ids:
        log.info("duplicate_skipped", job_id=job_id)
        return

    handler = _HANDLERS.get(job_type)
    if handler is None:
        log.warning("unknown_job_type", job_type=job_type, job_id=job_id)
        JOBS_PROCESSED.labels(status="error").inc()
        return  # commit and move on — no handler available

    with tracer.start_as_current_span(
        "worker.process",
        context=context,
        attributes={"messaging.system": "kafka", "messaging.destination.name": TOPIC_JOBS},
    ) as span:
        span.set_attribute("job.id", job_id)
        span.set_attribute("job.type", job_type)

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            start = time.perf_counter()
            try:
                result = handler(job)
                elapsed = time.perf_counter() - start
                JOB_DURATION.observe(elapsed)
                JOBS_PROCESSED.labels(status="success").inc()
                log.info(
                    "job_processed",
                    job_id=job_id,
                    job_type=job_type,
                    attempt=attempt,
                    duration_ms=round(elapsed * 1000, 2),
                    result=result,
                )
                if job_id:
                    seen_ids[job_id] = True
                    if len(seen_ids) > 10000:
                        seen_ids.popitem(last=False)
                return
            except Exception as exc:
                last_exc = exc
                span.record_exception(exc)
                log.warning(
                    "job_attempt_failed",
                    job_id=job_id,
                    job_type=job_type,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.1 * (2 ** (attempt - 1)))  # exponential back-off: 100ms, 200ms

        # All retries exhausted → send to DLQ
        dlq_payload = json.dumps(
            {"original_job": job, "error": str(last_exc), "attempts": MAX_RETRIES}
        ).encode()
        dlq_headers: list[tuple[str, bytes]] = []
        propagate.inject(dlq_headers, setter=kafka_header_setter)
        # Delivery-confirmed: if the DLQ publish itself fails, this raises out of
        # process_message — the run loop exits without committing, the pod
        # restarts, and the message is redelivered rather than silently dropped.
        produce_confirmed(
            producer,
            TOPIC_DLQ,
            value=dlq_payload,
            key=job_id.encode() if job_id else None,
            headers=dlq_headers,
        )
        JOBS_PROCESSED.labels(status="dlq").inc()
        log.error(
            "job_sent_to_dlq",
            job_id=job_id,
            job_type=job_type,
            error=str(last_exc),
        )
        if job_id:
            seen_ids[job_id] = True
            if len(seen_ids) > 10000:
                seen_ids.popitem(last=False)


def run() -> None:
    log.info("worker_starting", bootstrap=BOOTSTRAP_SERVERS, topic=TOPIC_JOBS, group=CONSUMER_GROUP)

    start_metrics_server()
    consumer = make_consumer()
    producer = make_producer()
    seen_ids = OrderedDict()
    shutdown = threading.Event()
    lag_last_polled = 0.0

    def _handle_signal(sig, frame):
        log.info("shutdown_signal_received", signal=sig)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not shutdown.is_set():
            msg = consumer.poll(timeout=1.0)

            # Periodically refresh lag gauge
            now = time.time()
            if now - lag_last_polled > 15:
                update_lag(consumer)
                lag_last_polled = now

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            process_message(msg.value(), producer, seen_ids, headers=msg.headers())

            # Manual commit — only after successful processing or DLQ publish
            consumer.commit(message=msg, asynchronous=False)

    finally:
        log.info("worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("worker_stopped")


if __name__ == "__main__":
    run()
