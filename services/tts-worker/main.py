"""
tts-worker — terminal pipeline stage.

Processing contract (docs/audio-pipeline-contract.md):
  - Consume `audio.summaries` (group `tts-worker`); validate the event —
    invalid messages go to `audio.tts-dlq` with the reason, then commit
  - Atomic Redis dedup claim (`dedup:tts:<job_id>`)
  - Read the summary JSON from S3 `summaries/<job_id>`
  - Synthesize speech via pluggable TTS_BACKEND (stub | kokoro)
  - Write WAV to S3 `speech/<job_id>`
  - Merge Redis job-state: status=done, keys.speech (TERMINAL)
  - Produce `audio.results` {job_id, speech_key, status:"done", created_at}
  - Permanent errors → DLQ immediately; transient → back-off + jitter, then DLQ
  - Commit only after the output event (or DLQ) is confirmed

As the TERMINAL stage this worker increments:
  pipeline_jobs_completed_total
  pipeline_end_to_end_duration_seconds  (now - created_at from the original envelope)
  pipeline_jobs_failed_total{stage="tts"}   (on DLQ)
"""

import json
import time

from app import config
from app.backends import _synthesize_kokoro, _synthesize_stub
from app.config import SERVICE_NAME, STAGE, STALE_SECONDS, settings
from app.metrics import (
    PIPELINE_COMPLETED,
    PIPELINE_E2E_DURATION,
    PIPELINE_FAILED,  # noqa: F401  (re-exported for tests/operators)
    PIPELINE_QUEUE_WAIT,  # noqa: F401
    TTS_GENERATION_DURATION,
    TTS_JOBS,
)
from platform_common.consumer_loop import (
    Outcome,
    install_signal_handlers,
    run_consumer_loop,
)
from platform_common.events import ResultEvent, SummaryEvent
from platform_common.health import start_metrics_server
from platform_common.job_state import make_redis_client
from platform_common.kafka_io import make_consumer, make_producer
from platform_common.observability import setup
from platform_common.pipeline_metrics import parse_created_at
from platform_common.retry import PermanentError, RetryPolicy
from platform_common.stage import StageConfig, StageResult, StageSpec, process_stage_message
from platform_common.storage import make_s3_client

log, tracer = setup(SERVICE_NAME)

# Module-level so tests/operators can inspect or monkeypatch them.
TOPIC_IN = config.TOPIC_IN
TOPIC_OUT = config.TOPIC_OUT
TOPIC_DLQ = config.TOPIC_DLQ
MAX_RETRIES = config.MAX_RETRIES
S3_BUCKET = config.S3_BUCKET
TTS_BACKEND = config.TTS_BACKEND

SPEC = StageSpec(
    name=STAGE,
    service=SERVICE_NAME,
    input_model=SummaryEvent,
    in_progress_status="synthesizing",
    jobs_total=TTS_JOBS,
)


def synthesize(text: str) -> bytes:
    """Dispatch to the configured TTS backend."""
    if TTS_BACKEND == "kokoro":
        return _synthesize_kokoro(
            text, lang_code=settings.kokoro_lang_code, voice=settings.kokoro_voice
        )
    return _synthesize_stub(text)


def stage_config() -> StageConfig:
    return StageConfig(
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        topic_dlq=TOPIC_DLQ,
        job_state_ttl_seconds=settings.job_state_ttl_seconds,
        dedup_lease_seconds=settings.effective_dedup_lease_seconds,
        policy=RetryPolicy.from_settings(settings, max_attempts=MAX_RETRIES),
    )


def _text_to_speak(summary_data) -> str:
    if not isinstance(summary_data, dict):
        raise PermanentError("summary document is not a JSON object")
    text = summary_data.get("summary")
    if not isinstance(text, str) or not text.strip():
        items = summary_data.get("action_items") or []
        text = " ".join(str(i) for i in items) or "No summary available."
    return text


def make_handler(s3_client):
    def handle(event: SummaryEvent) -> StageResult:
        resp = s3_client.get_object(Bucket=S3_BUCKET, Key=event.summary_key)
        text = _text_to_speak(json.loads(resp["Body"].read().decode("utf-8")))

        tts_start = time.perf_counter()
        audio_bytes = synthesize(text)
        tts_elapsed = time.perf_counter() - tts_start
        TTS_GENERATION_DURATION.observe(tts_elapsed)

        speech_key = f"speech/{event.job_id}"
        s3_client.put_object(
            Bucket=S3_BUCKET, Key=speech_key, Body=audio_bytes, ContentType="audio/wav"
        )
        return StageResult(
            out_event=ResultEvent(
                job_id=event.job_id, speech_key=speech_key, created_at=event.created_at
            ),
            state_update={
                "status": "done",
                "stage": STAGE,
                "keys": {"summary": event.summary_key, "speech": speech_key},
            },
            log_fields={
                "speech_key": speech_key,
                "tts_duration_ms": round(tts_elapsed * 1000, 2),
            },
        )

    return handle


def _on_success(event: SummaryEvent, _result: StageResult) -> None:
    """Terminal-stage pipeline metrics."""
    PIPELINE_COMPLETED.inc()
    ts = parse_created_at(event.created_at)
    if ts is not None:
        PIPELINE_E2E_DURATION.observe(max(0.0, time.time() - ts))


def process_message(
    raw_value: bytes,
    producer,
    s3_client,
    redis_client,
    headers: list[tuple[str, bytes]] | None = None,
    key: bytes | None = None,
) -> Outcome:
    return process_stage_message(
        SPEC,
        stage_config(),
        raw_value=raw_value,
        producer=producer,
        redis_client=redis_client,
        handler=make_handler(s3_client),
        headers=headers,
        key=key,
        on_success=_on_success,
    )


def run() -> None:
    log.info(
        "tts_worker_starting",
        bootstrap=settings.bootstrap_servers,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        backend=TTS_BACKEND,
    )
    start_metrics_server(settings.metrics_port, STALE_SECONDS)
    consumer = make_consumer(settings, SERVICE_NAME, [TOPIC_IN])
    producer = make_producer(settings, SERVICE_NAME)
    s3_client = make_s3_client(region=settings.s3_region, endpoint_url=settings.s3_endpoint_url)
    redis_client = make_redis_client(settings.redis_url, settings.redis_password.get_secret_value())
    install_signal_handlers()  # SIGTERM → SHUTDOWN: finish/abort in-flight work, exit
    try:
        run_consumer_loop(
            consumer,
            lambda msg: process_message(
                msg.value(), producer, s3_client, redis_client, headers=msg.headers(), key=msg.key()
            ),
        )
    finally:
        log.info("tts_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("tts_worker_stopped")


if __name__ == "__main__":
    run()
