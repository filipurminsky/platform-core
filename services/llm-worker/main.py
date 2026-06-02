"""
llm-worker — Kafka consumer that summarises meeting transcripts via the LLM gateway.

Processing contract (per audio-pipeline-contract.md §1-9, especially §6):
  - Consumes from `audio.transcripts` (consumer group `llm-worker`)
  - Reads full transcript text from S3 transcripts/<job_id>
  - Calls the EXISTING llm-gateway at LLM_GATEWAY_URL (OpenAI-compatible
    POST /v1/chat/completions).  NEVER calls vLLM directly.
    In dev the gateway proxies to TinyLlama — low quality by design; the pipeline
    is a wiring demo, not a production-quality summariser.
  - Parses the assistant reply defensively: extracts executive summary + action_items[];
    if parsing fails falls back to full text as summary + empty action_items (no crash).
  - Writes full summary JSON to S3 summaries/<job_id> (idempotent).
  - SETs Redis job-state: status=summarizing on start, keys.summary on success, failed on DLQ.
  - Produces `audio.summaries` (carries created_at forward for end-to-end metric).
  - On repeated failure: produces to `audio.llm-dlq` + sets Redis failed.
  - Manual commit only after output event/DLQ produced (enable.auto.commit=false).
  - SIGTERM: finishes in-flight message, commits, exits (terminationGracePeriodSeconds=60).

This module is the thin orchestration layer: per-job processing and the consume
loop. Supporting concerns live in the `app` package (config, observability,
metrics, kafka_io, storage, job_state, gateway).

Prometheus metrics on :9090/metrics (contract §7):
  llm_jobs_total{status}          Counter   success|error|dlq
  llm_tokens_generated_total      Counter   completion_tokens from gateway response
  llm_request_duration_seconds    Histogram latency of the /v1/chat/completions call
  pipeline_jobs_failed_total{stage="llm"}  Counter  on DLQ
"""

import json
import signal
import threading
import time
from datetime import UTC, datetime

import httpx
from app.config import (
    BOOTSTRAP_SERVERS,
    CONSUMER_GROUP,
    LLM_GATEWAY_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
    MAX_RETRIES,
    S3_BUCKET,
    TOPIC_DLQ,
    TOPIC_IN,
    TOPIC_OUT,
)
from app.gateway import _parse_llm_response, call_llm_gateway  # noqa: F401  (_parse re-exported)
from app.job_state import make_redis_client, mark_done, mark_failed, mark_summarizing
from app.kafka_io import kafka_header_getter, kafka_header_setter, make_consumer, make_producer
from app.metrics import LLM_JOBS, LLM_TOKENS, PIPELINE_FAILED
from app.metrics_server import start_metrics_server
from app.observability import log, tracer
from app.storage import make_s3_client
from confluent_kafka import KafkaError, KafkaException
from opentelemetry import propagate


def process_message(
    raw_value: bytes,
    producer,
    seen_ids: set,
    s3,
    r,
    http_client: httpx.Client,
    headers: list[tuple[str, bytes]] | None = None,
) -> None:
    """Deserialise → deduplicate → summarise → write S3 → Redis → produce → DLQ on failure."""
    context = propagate.extract(headers or [], getter=kafka_header_getter)

    try:
        event = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        log.error("bad_json", error=str(exc), raw=raw_value[:200])
        LLM_JOBS.labels(status="error").inc()
        return  # malformed message: commit and move on

    job_id = event.get("job_id", "")
    transcript_key = event.get("transcript_key", f"transcripts/{job_id}")
    created_at = event.get("created_at", datetime.now(UTC).isoformat())

    # Deduplication — in-memory (within a pod lifetime)
    if job_id and job_id in seen_ids:
        log.info("duplicate_skipped", job_id=job_id)
        return
    if job_id:
        seen_ids.add(job_id)

    with tracer.start_as_current_span(
        "llm-worker.process",
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
                # Mark in-progress in Redis
                mark_summarizing(r, job_id)

                # Read transcript from S3
                obj = s3.get_object(Bucket=S3_BUCKET, Key=transcript_key)
                transcript = obj["Body"].read().decode("utf-8")

                # Call LLM gateway (never vLLM directly — §6)
                summary, action_items, completion_tokens = call_llm_gateway(http_client, transcript)
                LLM_TOKENS.inc(completion_tokens)

                # Build full summary JSON for S3
                summary_key = f"summaries/{job_id}"
                summary_doc = {
                    "job_id": job_id,
                    "summary": summary,
                    "action_items": action_items,
                    "model": LLM_MODEL,
                    "created_at": created_at,
                }
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=summary_key,
                    Body=json.dumps(summary_doc).encode("utf-8"),
                    ContentType="application/json",
                )

                # Update Redis with summary key
                mark_done(r, job_id, summary_key)

                # Produce audio.summaries (carry created_at forward for end-to-end metric)
                out_event = {
                    "job_id": job_id,
                    "summary_key": summary_key,
                    "action_items": action_items,
                    "created_at": created_at,
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

                LLM_JOBS.labels(status="success").inc()
                log.info(
                    "job_summarised",
                    job_id=job_id,
                    attempt=attempt,
                    completion_tokens=completion_tokens,
                    action_items_count=len(action_items),
                )
                return

            except Exception as exc:
                last_exc = exc
                span.record_exception(exc)
                log.warning(
                    "job_attempt_failed",
                    job_id=job_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.1 * (2 ** (attempt - 1)))  # 100ms, 200ms back-off

        # All retries exhausted → dead-letter
        dlq_payload = json.dumps(
            {
                **event,
                "error": str(last_exc),
                "stage": "llm",
                "failed_at": datetime.now(UTC).isoformat(),
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

        mark_failed(r, job_id, str(last_exc))
        LLM_JOBS.labels(status="dlq").inc()
        PIPELINE_FAILED.labels(stage="llm").inc()
        log.error(
            "job_sent_to_dlq",
            job_id=job_id,
            error=str(last_exc),
        )


def run() -> None:
    log.info(
        "llm_worker_starting",
        bootstrap=BOOTSTRAP_SERVERS,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        group=CONSUMER_GROUP,
        llm_gateway=LLM_GATEWAY_URL,
    )

    start_metrics_server()
    consumer = make_consumer()
    producer = make_producer()
    s3 = make_s3_client()
    r = make_redis_client()
    http_client = httpx.Client(
        base_url=LLM_GATEWAY_URL,
        timeout=httpx.Timeout(LLM_TIMEOUT),
    )

    seen_ids: set[str] = set()
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

            # Periodically refresh lag gauge (best-effort; metric not in contract §7 for llm)
            now = time.time()
            if now - lag_last_polled > 15:
                lag_last_polled = now

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            process_message(
                msg.value(),
                producer,
                seen_ids,
                s3,
                r,
                http_client,
                headers=msg.headers(),
            )

            # Manual commit — only after output event or DLQ publish (§1)
            consumer.commit(message=msg, asynchronous=False)

    finally:
        log.info("llm_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        http_client.close()
        log.info("llm_worker_stopped")


if __name__ == "__main__":
    run()
