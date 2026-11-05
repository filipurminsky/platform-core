"""
worker-service — Kafka consumer that processes jobs from the `jobs` topic.

Processing contract:
  - Validates each message as a `Job` ({"id": str, "type": <registered>, "payload": {}})
  - Invalid messages (bad JSON, missing id, unknown type) → `jobs-dlq` with the
    raw payload + reason, then commit. Never silently dropped.
  - Runs the registered handler for job["type"]
  - Permanent errors (e.g. ValueError for a bad operation) → DLQ immediately;
    transient errors → exponential back-off with jitter (bounded by MAX_RETRIES
    and RETRY_BUDGET_SECONDS), then DLQ
  - Commits the offset manually (enable.auto.commit=false) only after success
    or a delivery-confirmed DLQ publish
  - Deduplication is **best-effort and in-memory**: an LRU of the last
    DEDUP_CACHE_SIZE job ids *per pod*. It does NOT survive a restart and is
    not shared across replicas — a message redelivered after a crash/rebalance
    can be processed again, so handlers must be idempotent (at-least-once).
    (The audio-pipeline workers use an atomic Redis claim instead; this demo
    service deliberately has no Redis dependency.)
  - Drains gracefully on SIGTERM: finishes the in-flight message, commits, exits

Prometheus metrics on :9090/metrics:
  worker_jobs_processed_total{status="success|error|dlq"}
  worker_job_duration_seconds (histogram)
  worker_consumer_lag{partition} (gauge, polled from assignment watermarks)
"""

import time
from collections import OrderedDict

from app import config
from app.config import SERVICE_NAME, STALE_SECONDS, settings
from app.handlers import _HANDLERS, handle_data_transform, handle_ping, register  # noqa: F401
from app.metrics import CONSUMER_LAG, JOB_DURATION, JOBS_PROCESSED
from opentelemetry import propagate
from platform_common.consumer_loop import (
    SHUTDOWN,
    Outcome,
    install_signal_handlers,
    run_consumer_loop,
)
from platform_common.events import InvalidMessage, build_dlq_payload, decode_json_object
from platform_common.health import start_metrics_server, touch
from platform_common.kafka_io import (
    kafka_header_getter,
    kafka_header_setter,
    make_consumer,
    make_producer,
    produce_confirmed,
    update_lag,
)
from platform_common.observability import setup
from platform_common.retry import RetryExhausted, RetryPolicy, call_with_retry
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log, tracer = setup(SERVICE_NAME)

# Re-exported for tests/operators (module-level so they can be monkeypatched).
TOPIC_JOBS = config.TOPIC_JOBS
TOPIC_DLQ = config.TOPIC_DLQ
MAX_RETRIES = config.MAX_RETRIES
DEDUP_CACHE_SIZE = config.DEDUP_CACHE_SIZE


class Job(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=256)
    type: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)


def retry_policy() -> RetryPolicy:
    return RetryPolicy.from_settings(settings, max_attempts=MAX_RETRIES)


def _remember(seen_ids: OrderedDict, job_id: str) -> None:
    seen_ids[job_id] = True
    seen_ids.move_to_end(job_id)
    while len(seen_ids) > DEDUP_CACHE_SIZE:
        seen_ids.popitem(last=False)


def _dead_letter(producer, *, original, raw_value, error, attempts, error_type, key) -> None:
    dlq_headers: list[tuple[str, bytes]] = []
    propagate.inject(dlq_headers, setter=kafka_header_setter)
    # Historical worker-service DLQ shape ({"original_job": ...}) + additive
    # envelope fields; unparseable input is carried as `raw_b64`.
    value = build_dlq_payload(
        original=None if original is None else {"original_job": original},
        raw_value=raw_value,
        error=error,
        stage=SERVICE_NAME,
        attempts=attempts,
        error_type=error_type,
    )
    # Delivery-confirmed: if the DLQ publish fails this raises out of
    # process_message — the loop exits without committing and the message is
    # redelivered rather than silently dropped.
    produce_confirmed(producer, TOPIC_DLQ, value=value, key=key, headers=dlq_headers)
    JOBS_PROCESSED.labels(status="dlq").inc()


def _validate(raw_value: bytes | None) -> Job:
    original = decode_json_object(raw_value)
    try:
        job = Job.model_validate(original)
    except ValidationError as exc:
        raise InvalidMessage(f"invalid job: {exc.errors()[0]['msg']}", original=original) from exc
    if job.type not in _HANDLERS:
        raise InvalidMessage(f"unknown job type: {job.type!r}", original=original)
    return job


def process_message(
    raw_value: bytes,
    producer,
    seen_ids: OrderedDict,
    headers: list[tuple[str, bytes]] | None = None,
    key: bytes | None = None,
) -> Outcome:
    """Validate → deduplicate → dispatch (with retry) → DLQ on failure."""
    context = propagate.extract(headers or [], getter=kafka_header_getter)

    try:
        job = _validate(raw_value)
    except InvalidMessage as exc:
        log.error("invalid_message", reason=exc.reason)
        JOBS_PROCESSED.labels(status="error").inc()
        _dead_letter(
            producer,
            original=exc.original,
            raw_value=raw_value,
            error=exc.reason,
            attempts=0,
            error_type="invalid",
            key=key,
        )
        return Outcome.COMMIT

    if job.id in seen_ids:
        log.info("duplicate_skipped", job_id=job.id)
        return Outcome.COMMIT

    handler = _HANDLERS[job.type]
    job_dict = job.model_dump()

    with tracer.start_as_current_span(
        "worker.process",
        context=context,
        attributes={"messaging.system": "kafka", "messaging.destination.name": TOPIC_JOBS},
    ) as span:
        span.set_attribute("job.id", job.id)
        span.set_attribute("job.type", job.type)

        def _on_failed(exc, attempt, ec, delay):
            span.record_exception(exc)
            log.warning(
                "job_attempt_failed",
                job_id=job.id,
                job_type=job.type,
                attempt=attempt,
                error=str(exc),
                retryable=ec.retryable,
                retry_in_s=None if delay is None else round(delay, 2),
            )

        attempts = 0

        def _attempt():
            nonlocal attempts
            attempts += 1
            start = time.perf_counter()
            result = handler(job_dict)
            JOB_DURATION.observe(time.perf_counter() - start)
            return result

        try:
            result = call_with_retry(
                _attempt,
                retry_policy(),
                on_attempt_failed=_on_failed,
                heartbeat=touch,
                abort=SHUTDOWN,
            )
        except RetryExhausted as exc:
            error_type = "permanent" if exc.permanent else "retries_exhausted"
            _dead_letter(
                producer,
                original=job_dict,
                raw_value=raw_value,
                error=str(exc.last_exc),
                attempts=exc.attempts,
                error_type=error_type,
                key=job.id.encode(),
            )
            log.error(
                "job_sent_to_dlq",
                job_id=job.id,
                job_type=job.type,
                error=str(exc.last_exc),
                error_type=error_type,
            )
            _remember(seen_ids, job.id)
            return Outcome.COMMIT

        JOBS_PROCESSED.labels(status="success").inc()
        log.info("job_processed", job_id=job.id, job_type=job.type, attempt=attempts, result=result)
        _remember(seen_ids, job.id)
        return Outcome.COMMIT


def run() -> None:
    log.info("worker_starting", bootstrap=config.BOOTSTRAP_SERVERS, topic=TOPIC_JOBS)
    start_metrics_server(config.METRICS_PORT, STALE_SECONDS)
    consumer = make_consumer(settings, SERVICE_NAME, [TOPIC_JOBS])
    producer = make_producer(settings, SERVICE_NAME)
    seen_ids: OrderedDict = OrderedDict()
    install_signal_handlers()  # SIGTERM → SHUTDOWN: finish/abort in-flight work, exit
    lag_last_polled = [0.0]

    def _tick():
        now = time.monotonic()
        if now - lag_last_polled[0] > 15:
            update_lag(consumer, CONSUMER_LAG)
            lag_last_polled[0] = now

    try:
        run_consumer_loop(
            consumer,
            lambda msg: process_message(
                msg.value(), producer, seen_ids, headers=msg.headers(), key=msg.key()
            ),
            on_tick=_tick,
        )
    finally:
        log.info("worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("worker_stopped")


if __name__ == "__main__":
    run()
