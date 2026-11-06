"""
stt-worker — Kafka consumer that transcribes audio jobs.

Processing contract (docs/audio-pipeline-contract.md):
  - Consume `audio.jobs` (group `stt-worker`); validate the event (§3) —
    invalid messages go to `audio.stt-dlq` with the reason, then commit
  - Atomic Redis dedup claim (`dedup:stt:<job_id>`), then read audio from S3
  - Transcribe via pluggable STT_BACKEND (stub | nemo)
  - Write the full transcript to S3 `transcripts/<job_id>`
  - Merge Redis job-state (status transcribing → keys.transcript)
  - Produce `audio.transcripts` (carries created_at forward, §7)
  - Permanent errors → DLQ immediately; transient → back-off + jitter, then DLQ
  - Commit offset ONLY after the output event (or DLQ) is confirmed

The per-message protocol lives in `platform_common.stage`; this module holds
the stage-specific work (`handle`) and wiring.

Prometheus metrics on :9090/metrics:
  stt_jobs_total{status}  stt_job_duration_seconds  stt_errors_total
  stt_transcript_bytes    pipeline_jobs_failed_total{stage="stt"}
  pipeline_queue_wait_seconds{stage="stt"}
"""

import time

from app import config
from app.backends import _transcribe_nemo, _transcribe_stub
from app.config import SERVICE_NAME, STAGE, STALE_SECONDS, settings
from app.metrics import (
    PIPELINE_JOBS_FAILED,  # noqa: F401  (re-exported for tests/operators)
    PIPELINE_QUEUE_WAIT,  # noqa: F401
    STT_ERRORS_TOTAL,
    STT_JOB_DURATION,
    STT_JOBS_TOTAL,
    STT_TRANSCRIPT_BYTES,
)
from platform_common.consumer_loop import (
    Outcome,
    install_signal_handlers,
    run_consumer_loop,
)
from platform_common.events import AudioJobEvent, TranscriptEvent, now_iso
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
STT_BACKEND = config.STT_BACKEND

SPEC = StageSpec(
    name=STAGE,
    service=SERVICE_NAME,
    input_model=AudioJobEvent,
    in_progress_status="transcribing",
    jobs_total=STT_JOBS_TOTAL,
    errors_total=STT_ERRORS_TOTAL,
)


def transcribe(audio_bytes: bytes) -> tuple[str, str, float]:
    """Dispatch to the configured STT backend."""
    if STT_BACKEND == "nemo":
        return _transcribe_nemo(
            audio_bytes, model_path=settings.nemo_model_path, ffmpeg=settings.ffmpeg_binary
        )
    return _transcribe_stub(audio_bytes)


def stage_config() -> StageConfig:
    return StageConfig(
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        topic_dlq=TOPIC_DLQ,
        job_state_ttl_seconds=settings.job_state_ttl_seconds,
        dedup_lease_seconds=settings.effective_dedup_lease_seconds,
        policy=RetryPolicy.from_settings(settings, max_attempts=MAX_RETRIES),
    )


def make_handler(s3_client):
    def handle(event: AudioJobEvent) -> StageResult:
        start = time.perf_counter()
        audio_bytes = s3_client.get_object(Bucket=S3_BUCKET, Key=event.audio_key)["Body"].read()
        transcript_text, language, duration_s = transcribe(audio_bytes)

        # Idempotent write — deterministic key.
        transcript_key = f"transcripts/{event.job_id}"
        transcript_bytes = transcript_text.encode("utf-8")
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=transcript_key,
            Body=transcript_bytes,
            ContentType="text/plain; charset=utf-8",
        )
        elapsed = time.perf_counter() - start
        STT_JOB_DURATION.observe(elapsed)
        STT_TRANSCRIPT_BYTES.observe(len(transcript_bytes))

        return StageResult(
            out_event=TranscriptEvent(
                job_id=event.job_id,
                transcript_key=transcript_key,
                language=language,
                duration_s=duration_s,
                # Carried forward to the terminal stage for the end-to-end metric.
                created_at=event.created_at or now_iso(),
            ),
            state_update={
                "status": "transcribing",
                "stage": STAGE,
                "keys": {"transcript": transcript_key},
            },
            log_fields={
                "transcript_key": transcript_key,
                "language": language,
                "duration_s": duration_s,
                "elapsed_ms": round(elapsed * 1000, 2),
            },
        )

    return handle


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
    )


def run() -> None:
    log.info(
        "stt_worker_starting",
        bootstrap=settings.bootstrap_servers,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        dlq=TOPIC_DLQ,
        backend=STT_BACKEND,
    )
    start_metrics_server(settings.metrics_port, STALE_SECONDS)
    consumer = make_consumer(settings, SERVICE_NAME, [TOPIC_IN])
    producer = make_producer(settings, SERVICE_NAME)
    s3 = make_s3_client(region=settings.s3_region, endpoint_url=settings.s3_endpoint_url)
    redis = make_redis_client(settings.redis_url, settings.redis_password.get_secret_value())
    install_signal_handlers()  # SIGTERM → SHUTDOWN: finish/abort in-flight work, exit
    try:
        run_consumer_loop(
            consumer,
            lambda msg: process_message(
                msg.value(), producer, s3, redis, headers=msg.headers(), key=msg.key()
            ),
        )
    finally:
        log.info("stt_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("stt_worker_stopped")


if __name__ == "__main__":
    run()
