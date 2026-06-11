"""
tts-worker — terminal pipeline stage.

Processing contract (audio-pipeline-contract.md §1-9):
  - Consumes from `audio.summaries` (group `tts-worker`)
  - Reads summary JSON from S3 summaries/<job_id>
  - Synthesizes spoken audio via pluggable backend (TTS_BACKEND=stub|kokoro)
  - Writes WAV to S3 speech/<job_id>
  - Sets Redis job-state: status=done, stage=tts, keys.speech set  (TERMINAL)
  - Produces `audio.results` {job_id, speech_key, status:"done", created_at}
  - On repeated failure → `audio.tts-dlq` + Redis failed error.{stage:tts,...}
  - Manual commit only after output or DLQ is produced
  - Drains gracefully on SIGTERM

This module is the thin orchestration layer: backend dispatch, per-job
processing, and the consume loop. Supporting concerns live in the `app`
package (config, observability, metrics, kafka_io, storage, job_state,
backends).

As the TERMINAL stage this worker increments:
  pipeline_jobs_completed_total
  pipeline_end_to_end_duration_seconds  (now - created_at from original envelope)
  pipeline_jobs_failed_total{stage="tts"}   (on DLQ)

Metrics exposed on :9090/metrics:
  tts_jobs_total{status}                Counter
  tts_generation_duration_seconds       Histogram
  pipeline_jobs_completed_total         Counter
  pipeline_end_to_end_duration_seconds  Histogram
  pipeline_jobs_failed_total{stage}     Counter
"""

from __future__ import annotations

import json
import signal
import threading
import time
from datetime import UTC, datetime

from app.backends import _synthesize_kokoro, _synthesize_stub
from app.config import (
    BOOTSTRAP_SERVERS,
    CONSUMER_GROUP,
    MAX_RETRIES,
    S3_BUCKET,
    TOPIC_DLQ,
    TOPIC_IN,
    TOPIC_OUT,
    TTS_BACKEND,
)
from app.job_state import make_redis_client, now_iso, set_done, set_failed, set_synthesizing
from app.kafka_io import (
    kafka_header_getter,
    kafka_header_setter,
    make_consumer,
    make_producer,
    produce_confirmed,
)
from app.metrics import (
    PIPELINE_COMPLETED,
    PIPELINE_E2E_DURATION,
    PIPELINE_FAILED,
    PIPELINE_QUEUE_WAIT,
    TTS_GENERATION_DURATION,
    TTS_JOBS,
)
from app.metrics_server import start_metrics_server
from app.metrics_server import touch as _heartbeat
from app.observability import log, tracer
from app.storage import make_s3_client
from confluent_kafka import KafkaError, KafkaException
from opentelemetry import propagate


def synthesize(text: str) -> bytes:
    """Dispatch to the configured TTS backend."""
    if TTS_BACKEND == "kokoro":
        return _synthesize_kokoro(text)
    # default: stub
    return _synthesize_stub(text)


def _parse_created_at(created_at: str) -> float | None:
    """Return a UTC timestamp float from an ISO-8601 string, or None on parse error."""
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def process_message(
    raw_value: bytes,
    producer,
    s3_client,
    redis_client,
    headers: list[tuple[str, bytes]] | None = None,
) -> None:
    """Deserialise → deduplicate → synthesize → S3 → Redis → produce → DLQ on failure."""
    context = propagate.extract(headers or [], getter=kafka_header_getter)

    try:
        event = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        log.error("bad_json", error=str(exc), raw=raw_value[:200])
        TTS_JOBS.labels(status="error").inc()
        return  # malformed — commit and move on

    job_id = event.get("job_id", "")
    summary_key = event.get("summary_key", f"summaries/{job_id}")
    created_at: str = event.get("created_at", "")

    # Deduplication — Redis-based (survives restarts/rebalances).
    if job_id:
        try:
            if redis_client.get(f"dedup:tts:{job_id}"):
                log.info("duplicate_skipped", job_id=job_id)
                return
        except Exception as exc:
            log.warning("dedup_check_failed", job_id=job_id, error=str(exc))

    # Queue wait — how long the job sat between creation and this stage starting (§7).
    _qw_ts = _parse_created_at(created_at)
    if _qw_ts is not None:
        PIPELINE_QUEUE_WAIT.labels(stage="tts").observe(max(0.0, time.time() - _qw_ts))

    speech_key = f"speech/{job_id}"

    with tracer.start_as_current_span(
        "tts-worker.process",
        context=context,
        attributes={
            "messaging.system": "kafka",
            "messaging.destination.name": TOPIC_IN,
            "job.id": job_id,
        },
    ) as span:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Mark job as synthesizing in Redis
                set_synthesizing(redis_client, job_id, summary_key)

                # Read summary JSON from S3
                log.info("reading_summary", job_id=job_id, key=summary_key, attempt=attempt)
                resp = s3_client.get_object(Bucket=S3_BUCKET, Key=summary_key)
                summary_json = resp["Body"].read().decode()
                summary_data = json.loads(summary_json)

                # Extract text to synthesize (executive_summary or action_items fallback)
                text = summary_data.get(
                    "summary",
                    " ".join(summary_data.get("action_items", ["No summary available."])),
                )

                # Synthesize
                tts_start = time.perf_counter()
                audio_bytes = synthesize(text)
                tts_elapsed = time.perf_counter() - tts_start
                TTS_GENERATION_DURATION.observe(tts_elapsed)

                # Write speech to S3
                log.info("writing_speech", job_id=job_id, key=speech_key)
                s3_client.put_object(
                    Bucket=S3_BUCKET,
                    Key=speech_key,
                    Body=audio_bytes,
                    ContentType="audio/wav",
                )

                # Set terminal Redis state: status=done
                set_done(redis_client, job_id, summary_key, speech_key)

                # Produce audio.results
                out_event = {
                    "job_id": job_id,
                    "speech_key": speech_key,
                    "status": "done",
                    "created_at": created_at,
                }
                out_headers: list[tuple[str, bytes]] = []
                propagate.inject(out_headers, setter=kafka_header_setter)
                # Delivery-confirmed: raises (→ retry/DLQ) if the broker did not
                # ack, so the offset commit below never outruns the output event.
                produce_confirmed(
                    producer,
                    TOPIC_OUT,
                    value=json.dumps(out_event).encode(),
                    key=job_id.encode() if job_id else None,
                    headers=out_headers,
                )

                # Pipeline terminal metrics
                TTS_JOBS.labels(status="success").inc()
                PIPELINE_COMPLETED.inc()

                ts = _parse_created_at(created_at)
                if ts is not None:
                    e2e = time.time() - ts
                    PIPELINE_E2E_DURATION.observe(e2e)

                log.info(
                    "tts_job_done",
                    job_id=job_id,
                    speech_key=speech_key,
                    tts_duration_ms=round(tts_elapsed * 1000, 2),
                    attempt=attempt,
                )
                span.set_attribute("speech.key", speech_key)
                if job_id:
                    try:
                        redis_client.setex(f"dedup:tts:{job_id}", 86400, "1")
                    except Exception as exc:
                        log.warning("dedup_set_failed", job_id=job_id, error=str(exc))
                return

            except Exception as exc:
                last_exc = exc
                span.record_exception(exc)
                log.warning(
                    "tts_attempt_failed",
                    job_id=job_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.1 * (2 ** (attempt - 1)))  # 100ms, 200ms

        # All retries exhausted → DLQ
        failed_at = now_iso()
        dlq_payload = json.dumps(
            {
                **event,
                "error": str(last_exc),
                "stage": "tts",
                "failed_at": failed_at,
                "attempts": MAX_RETRIES,
            }
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

        # Redis: mark failed
        set_failed(redis_client, job_id, str(last_exc))

        TTS_JOBS.labels(status="dlq").inc()
        PIPELINE_FAILED.labels(stage="tts").inc()
        log.error(
            "tts_job_dlq",
            job_id=job_id,
            error=str(last_exc),
            dlq_topic=TOPIC_DLQ,
        )
        if job_id:
            try:
                redis_client.setex(f"dedup:tts:{job_id}", 86400, "1")
            except Exception as exc:
                log.warning("dedup_set_failed", job_id=job_id, error=str(exc))


def run() -> None:
    log.info(
        "tts_worker_starting",
        bootstrap=BOOTSTRAP_SERVERS,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        group=CONSUMER_GROUP,
        backend=TTS_BACKEND,
    )

    start_metrics_server()
    consumer = make_consumer()
    producer = make_producer()
    s3_client = make_s3_client()
    redis_client = make_redis_client()
    shutdown = threading.Event()

    def _handle_signal(sig, frame):
        log.info("shutdown_signal_received", signal=sig)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not shutdown.is_set():
            msg = consumer.poll(timeout=1.0)
            _heartbeat()

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            process_message(
                msg.value(),
                producer,
                s3_client,
                redis_client,
                headers=msg.headers(),
            )

            # Manual commit — only after output or DLQ produced
            try:
                consumer.commit(message=msg, asynchronous=False)
            except KafkaException as exc:
                log.warning("offset_commit_failed", error=str(exc))

    finally:
        log.info("tts_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("tts_worker_stopped")


if __name__ == "__main__":
    run()
