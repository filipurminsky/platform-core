"""
stt-worker — Kafka consumer that transcribes audio jobs.

Processing contract (audio-pipeline-contract.md §3):
  - Consume from `audio.jobs` (group `stt-worker`)
  - Read audio blob from S3 key audio/<job_id>
  - Transcribe via pluggable STT_BACKEND (stub | nemo)
  - Write full transcript text to S3 `transcripts/<job_id>`
  - SET Redis job-state (status transcribing → keys.transcript set)
  - Produce `audio.transcripts` event (carry created_at forward per §7)
  - On repeated failure: produce to `audio.stt-dlq`, SET Redis failed
  - Commit offset ONLY after the output event (or DLQ) is produced

This module is the thin orchestration layer: backend dispatch, per-job
processing, and the consume loop. Supporting concerns live in the `app`
package (config, observability, metrics, kafka_io, storage, job_state,
backends).

Prometheus metrics on :9090/metrics:
  stt_jobs_total{status}          Counter
  stt_job_duration_seconds        Histogram
  stt_errors_total                Counter
  pipeline_jobs_failed_total{stage="stt"}  Counter (shared across workers)
"""

import json
import signal
import threading
import time
from datetime import UTC, datetime

from app.backends import _transcribe_nemo, _transcribe_stub
from app.config import (
    BOOTSTRAP_SERVERS,
    CONSUMER_GROUP,
    MAX_RETRIES,
    S3_BUCKET,
    STT_BACKEND,
    TOPIC_DLQ,
    TOPIC_IN,
    TOPIC_OUT,
)
from app.job_state import make_redis_client, set_job_state
from app.kafka_io import kafka_header_getter, kafka_header_setter, make_consumer, make_producer
from app.metrics import PIPELINE_JOBS_FAILED, STT_ERRORS_TOTAL, STT_JOB_DURATION, STT_JOBS_TOTAL
from app.metrics_server import start_metrics_server
from app.observability import log, tracer
from app.storage import make_s3_client
from confluent_kafka import KafkaError, KafkaException
from opentelemetry import propagate


def transcribe(audio_bytes: bytes) -> tuple[str, str, float]:
    """Dispatch to the configured STT backend."""
    if STT_BACKEND == "nemo":
        return _transcribe_nemo(audio_bytes)
    # Default: stub
    return _transcribe_stub(audio_bytes)


def process_message(
    raw_value: bytes,
    producer,
    s3,
    redis,
    seen_ids: set,
    headers: list[tuple[str, bytes]] | None = None,
) -> None:
    """
    Deserialise → deduplicate → transcribe → write S3 → update Redis →
    produce audio.transcripts → commit.
    On repeated failure: produce to audio.stt-dlq, update Redis to failed.
    """
    # Extract trace context from Kafka headers — stitch into the distributed trace
    context = propagate.extract(headers or [], getter=kafka_header_getter)

    try:
        event = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        log.error("bad_json", error=str(exc), raw=raw_value[:200])
        STT_ERRORS_TOTAL.inc()
        return  # malformed message: commit and move on

    job_id = event.get("job_id", "")
    audio_key = event.get("audio_key", f"audio/{job_id}")
    created_at = event.get("created_at", datetime.now(UTC).isoformat())

    # Deduplication — in-memory (within a pod lifetime)
    if job_id and job_id in seen_ids:
        log.info("duplicate_skipped", job_id=job_id)
        return
    if job_id:
        seen_ids.add(job_id)

    with tracer.start_as_current_span(
        "stt.process",
        context=context,
        attributes={
            "messaging.system": "kafka",
            "messaging.destination.name": TOPIC_IN,
            "job.id": job_id,
        },
    ) as span:
        # Mark job as in-progress
        try:
            set_job_state(redis, job_id, {"status": "transcribing", "stage": "stt"})
        except Exception as exc:
            log.warning("redis_state_update_failed", job_id=job_id, error=str(exc))

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            start = time.perf_counter()
            try:
                # 1. Read audio from S3
                audio_bytes = s3.get_object(Bucket=S3_BUCKET, Key=audio_key)["Body"].read()

                # 2. Transcribe
                transcript_text, language, duration_s = transcribe(audio_bytes)

                # 3. Write transcript to S3 (idempotent — deterministic key)
                transcript_key = f"transcripts/{job_id}"
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=transcript_key,
                    Body=transcript_text.encode("utf-8"),
                    ContentType="text/plain",
                )

                elapsed = time.perf_counter() - start
                STT_JOB_DURATION.observe(elapsed)

                # 4. Update Redis: set transcript key in state
                try:
                    set_job_state(
                        redis,
                        job_id,
                        {
                            "status": "transcribing",
                            "stage": "stt",
                            "keys": {"transcript": transcript_key},
                        },
                    )
                except Exception as exc:
                    log.warning("redis_state_update_failed", job_id=job_id, error=str(exc))

                # 5. Produce audio.transcripts event — carry created_at forward (§7)
                out_event = {
                    "job_id": job_id,
                    "transcript_key": transcript_key,
                    "language": language,
                    "duration_s": duration_s,
                    "created_at": created_at,  # propagated to terminal stage for end-to-end metric
                }
                out_headers: list[tuple[str, bytes]] = []
                propagate.inject(out_headers, setter=kafka_header_setter)

                producer.produce(
                    TOPIC_OUT,
                    value=json.dumps(out_event).encode(),
                    key=job_id.encode() if job_id else None,
                    headers=out_headers,
                )
                producer.flush(timeout=5)

                STT_JOBS_TOTAL.labels(status="success").inc()
                log.info(
                    "job_processed",
                    job_id=job_id,
                    transcript_key=transcript_key,
                    language=language,
                    duration_s=duration_s,
                    attempt=attempt,
                    elapsed_ms=round(elapsed * 1000, 2),
                )
                return

            except Exception as exc:
                last_exc = exc
                span.record_exception(exc)
                STT_ERRORS_TOTAL.inc()
                log.warning(
                    "job_attempt_failed",
                    job_id=job_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.1 * (2 ** (attempt - 1)))  # exponential back-off: 100ms, 200ms

        # All retries exhausted → send to DLQ (contract §3)
        failed_at = datetime.now(UTC).isoformat()
        dlq_payload = json.dumps(
            {
                **event,
                "error": str(last_exc),
                "stage": "stt",
                "failed_at": failed_at,
                "attempts": MAX_RETRIES,
            }
        ).encode()
        dlq_headers: list[tuple[str, bytes]] = []
        propagate.inject(dlq_headers, setter=kafka_header_setter)
        producer.produce(
            TOPIC_DLQ,
            value=dlq_payload,
            key=job_id.encode() if job_id else None,
            headers=dlq_headers,
        )
        producer.flush(timeout=5)

        # Update Redis to failed
        try:
            set_job_state(
                redis,
                job_id,
                {
                    "status": "failed",
                    "stage": "stt",
                    "error": {
                        "stage": "stt",
                        "message": str(last_exc),
                        "dlq_topic": TOPIC_DLQ,
                    },
                },
            )
        except Exception as exc:
            log.warning("redis_failed_state_update_failed", job_id=job_id, error=str(exc))

        STT_JOBS_TOTAL.labels(status="dlq").inc()
        PIPELINE_JOBS_FAILED.labels(stage="stt").inc()
        log.error(
            "job_sent_to_dlq",
            job_id=job_id,
            dlq_topic=TOPIC_DLQ,
            error=str(last_exc),
        )


def run() -> None:
    log.info(
        "stt_worker_starting",
        bootstrap=BOOTSTRAP_SERVERS,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        dlq=TOPIC_DLQ,
        group=CONSUMER_GROUP,
        backend=STT_BACKEND,
    )

    start_metrics_server()
    consumer = make_consumer()
    producer = make_producer()
    s3 = make_s3_client()
    redis = make_redis_client()
    seen_ids: set[str] = set()
    shutdown = threading.Event()

    def _handle_signal(sig, frame):
        log.info("shutdown_signal_received", signal=sig)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not shutdown.is_set():
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            process_message(
                msg.value(),
                producer,
                s3,
                redis,
                seen_ids,
                headers=msg.headers(),
            )

            # Manual commit — only after successful processing or DLQ publish (contract §1)
            consumer.commit(message=msg, asynchronous=False)

    finally:
        log.info("stt_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("stt_worker_stopped")


if __name__ == "__main__":
    run()
