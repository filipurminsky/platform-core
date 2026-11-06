"""
llm-worker — Kafka consumer that summarises meeting transcripts via the LLM gateway.

Processing contract (docs/audio-pipeline-contract.md, especially §6):
  - Consume `audio.transcripts` (group `llm-worker`); validate the event —
    invalid messages go to `audio.llm-dlq` with the reason, then commit
  - Atomic Redis dedup claim (`dedup:llm:<job_id>`)
  - Read the full transcript from S3 `transcripts/<job_id>`
  - Call the EXISTING llm-gateway (POST /v1/chat/completions, bearer auth) —
    NEVER vLLM directly. Long transcripts are summarised map-reduce.
  - Gateway 429/502/503/504 / connection refused (vLLM scaling from zero) are
    retried for up to LLM_COLD_START_BUDGET_SECONDS honouring Retry-After;
    other 4xx are permanent → DLQ immediately
  - Write the summary JSON to S3 `summaries/<job_id>`; merge Redis keys.summary
  - Produce `audio.summaries` (carries created_at forward)
  - Commit only after the output event (or DLQ) is confirmed

Prometheus metrics on :9090/metrics (contract §7):
  llm_jobs_total{status}  llm_tokens_generated_total  llm_request_duration_seconds
  llm_summary_bytes  pipeline_jobs_failed_total{stage="llm"}
  pipeline_queue_wait_seconds{stage="llm"}
  (additive) llm_transcript_chunks  llm_transcript_truncated_total
"""

import json
import time

import httpx
from app import config
from app.config import SERVICE_NAME, STAGE, STALE_SECONDS, settings
from app.gateway import _parse_llm_response, call_llm_gateway, make_http_client  # noqa: F401
from app.metrics import (
    LLM_JOBS,
    LLM_SUMMARY_BYTES,
    LLM_TOKENS,
    PIPELINE_FAILED,  # noqa: F401  (re-exported for tests/operators)
    PIPELINE_QUEUE_WAIT,  # noqa: F401
)
from platform_common.consumer_loop import (
    Outcome,
    install_signal_handlers,
    run_consumer_loop,
)
from platform_common.events import SummaryEvent, TranscriptEvent, now_iso
from platform_common.health import start_metrics_server
from platform_common.job_state import make_redis_client
from platform_common.kafka_io import make_consumer, make_producer
from platform_common.observability import setup
from platform_common.retry import RetryPolicy
from platform_common.stage import StageConfig, StageResult, StageSpec, process_stage_message
from platform_common.storage import make_s3_client

log, tracer = setup(SERVICE_NAME)

# Module-level so tests/operators can inspect or monkeypatch them.
TOPIC_IN = config.TOPIC_IN
TOPIC_OUT = config.TOPIC_OUT
TOPIC_DLQ = config.TOPIC_DLQ
MAX_RETRIES = config.MAX_RETRIES
S3_BUCKET = config.S3_BUCKET
LLM_MODEL = config.LLM_MODEL
LLM_GATEWAY_URL = config.LLM_GATEWAY_URL

SPEC = StageSpec(
    name=STAGE,
    service=SERVICE_NAME,
    input_model=TranscriptEvent,
    in_progress_status="summarizing",
    jobs_total=LLM_JOBS,
)


def stage_config() -> StageConfig:
    return StageConfig(
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        topic_dlq=TOPIC_DLQ,
        job_state_ttl_seconds=settings.job_state_ttl_seconds,
        dedup_lease_seconds=settings.effective_dedup_lease_seconds,
        # retry_budget_seconds == LLM_COLD_START_BUDGET_SECONDS for this worker.
        policy=RetryPolicy.from_settings(settings, max_attempts=MAX_RETRIES),
    )


def make_handler(s3, http_client: httpx.Client):
    chunk_cache: dict = {}  # per message: survives retry attempts, not messages

    def handle(event: TranscriptEvent) -> StageResult:
        deadline = time.monotonic() + settings.attempt_timeout_seconds
        obj = s3.get_object(Bucket=S3_BUCKET, Key=event.transcript_key)
        transcript = obj["Body"].read().decode("utf-8")

        summary, action_items, completion_tokens = call_llm_gateway(
            http_client,
            transcript,
            deadline=deadline,
            cache=chunk_cache,
            log_ctx={"job_id": event.job_id},
        )
        LLM_TOKENS.inc(completion_tokens)

        created_at = event.created_at or now_iso()
        summary_key = f"summaries/{event.job_id}"
        summary_body = json.dumps(
            {
                "job_id": event.job_id,
                "summary": summary,
                "action_items": action_items,
                "model": LLM_MODEL,
                "created_at": created_at,
            }
        ).encode("utf-8")
        s3.put_object(
            Bucket=S3_BUCKET, Key=summary_key, Body=summary_body, ContentType="application/json"
        )
        LLM_SUMMARY_BYTES.observe(len(summary_body))

        return StageResult(
            out_event=SummaryEvent(
                job_id=event.job_id,
                summary_key=summary_key,
                action_items=action_items,
                created_at=created_at,
            ),
            # Status stays "summarizing" — tts-worker moves it on (§5 has no
            # "summarized" state).
            state_update={
                "status": "summarizing",
                "stage": STAGE,
                "keys": {"summary": summary_key},
            },
            log_fields={
                "completion_tokens": completion_tokens,
                "action_items_count": len(action_items),
                "transcript_chars": len(transcript),
            },
        )

    return handle


def process_message(
    raw_value: bytes,
    producer,
    s3,
    r,
    http_client: httpx.Client,
    headers: list[tuple[str, bytes]] | None = None,
    key: bytes | None = None,
) -> Outcome:
    return process_stage_message(
        SPEC,
        stage_config(),
        raw_value=raw_value,
        producer=producer,
        redis_client=r,
        handler=make_handler(s3, http_client),
        headers=headers,
        key=key,
    )


def run() -> None:
    log.info(
        "llm_worker_starting",
        bootstrap=settings.bootstrap_servers,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        llm_gateway=LLM_GATEWAY_URL,
        gateway_auth=bool(settings.llm_gateway_api_key),
        cold_start_budget_s=settings.retry_budget_seconds,
    )
    start_metrics_server(settings.metrics_port, STALE_SECONDS)
    consumer = make_consumer(settings, SERVICE_NAME, [TOPIC_IN])
    producer = make_producer(settings, SERVICE_NAME)
    s3 = make_s3_client(region=settings.s3_region, endpoint_url=settings.s3_endpoint_url)
    r = make_redis_client(settings.redis_url, settings.redis_password.get_secret_value())
    http_client = make_http_client()
    try:
        # Propagate W3C trace context on gateway calls (one trace across hops).
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor.instrument_client(http_client)
    except Exception as exc:  # pragma: no cover - tracing is best-effort
        log.warning("httpx_instrumentation_failed", error=str(exc))

    install_signal_handlers()  # SIGTERM → SHUTDOWN: finish/abort in-flight work, exit
    try:
        run_consumer_loop(
            consumer,
            lambda msg: process_message(
                msg.value(), producer, s3, r, http_client, headers=msg.headers(), key=msg.key()
            ),
        )
    finally:
        log.info("llm_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        http_client.close()
        log.info("llm_worker_stopped")


if __name__ == "__main__":
    run()
