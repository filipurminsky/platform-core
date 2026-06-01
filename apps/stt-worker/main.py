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

Pluggable STT backends:
  STT_BACKEND=stub  (default) — returns a placeholder transcript string,
      language "en", and a duration_s derived from audio byte size.
      No ML deps required; suitable for local dev and CI.
  STT_BACKEND=nemo  — lazy-imports nemo_toolkit Parakeet (GPU).
      IMPORTANT: nemo/torch are NOT in pyproject.toml. The prod GPU image
      bakes the Parakeet RNNT model + nemo/torch deps via a separate
      build stage (see Dockerfile). Never runs on CPU-only nodes.

Prometheus metrics on :9090/metrics:
  stt_jobs_total{status}          Counter
  stt_job_duration_seconds        Histogram
  stt_errors_total                Counter
  pipeline_jobs_failed_total{stage="stt"}  Counter (shared across workers)
"""

import json
import os
import signal
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import redis as redis_client
import structlog
from botocore.config import Config
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import Getter, Setter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Config (environment-driven, safe defaults for local dev)
# ---------------------------------------------------------------------------
BOOTSTRAP_SERVERS = os.getenv(
    "KAFKA_BOOTSTRAP_SERVERS",
    "localhost:9092",
)
TOPIC_IN = os.getenv("KAFKA_TOPIC_IN", "audio.jobs")
TOPIC_OUT = os.getenv("KAFKA_TOPIC_OUT", "audio.transcripts")
TOPIC_DLQ = os.getenv("KAFKA_DLQ_TOPIC", "audio.stt-dlq")
CONSUMER_GROUP = os.getenv("KAFKA_CONSUMER_GROUP", "stt-worker")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

# STT backend — stub (default, no ML deps) or nemo (GPU, lazy-imported)
STT_BACKEND = os.getenv("STT_BACKEND", "stub")

# SASL/SCRAM — injected by Strimzi KafkaUser secret in prod; empty → plaintext for local dev
SASL_USERNAME = os.getenv("KAFKA_SASL_USERNAME", "")
SASL_PASSWORD = os.getenv("KAFKA_SASL_PASSWORD", "")
SECURITY_PROTOCOL = os.getenv(
    "KAFKA_SECURITY_PROTOCOL", "SASL_SSL" if SASL_USERNAME else "PLAINTEXT"
)

# S3 — boto3 client config (§4)
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")  # set to MinIO URL in dev; unset in prod
S3_BUCKET = os.getenv("S3_BUCKET", "audio-pipeline")
S3_REGION = os.getenv("S3_REGION", "eu-west-1")

# Redis — job-state store (§5)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
JOB_STATE_TTL_SECONDS = int(os.getenv("JOB_STATE_TTL_SECONDS", "604800"))  # 7 days


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def add_trace_context(_logger, _method_name, event_dict):
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        add_trace_context,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
def configure_tracing() -> None:
    """Honour OTEL_SDK_DISABLED so tests/CI don't block on span export."""
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    service_name = os.getenv("OTEL_SERVICE_NAME", "stt-worker")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


configure_tracing()
tracer = trace.get_tracer(__name__)


class KafkaHeaderGetter(Getter):
    def get(self, carrier, key):
        values = []
        for header_key, header_value in carrier or []:
            if header_key.lower() == key.lower() and header_value is not None:
                values.append(
                    header_value.decode() if isinstance(header_value, bytes) else str(header_value)
                )
        return values or None

    def keys(self, carrier):
        return [key for key, _value in carrier or []]


class KafkaHeaderSetter(Setter):
    def set(self, carrier, key, value):
        carrier.append((key, value.encode()))


kafka_header_getter = KafkaHeaderGetter()
kafka_header_setter = KafkaHeaderSetter()

# ---------------------------------------------------------------------------
# Metrics (contract §7)
# ---------------------------------------------------------------------------
STT_JOBS_TOTAL = Counter(
    "stt_jobs_total",
    "STT jobs processed by outcome",
    ["status"],  # success | error | dlq
)
STT_JOB_DURATION = Histogram(
    "stt_job_duration_seconds",
    "Time spent transcribing a single job",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)
STT_ERRORS_TOTAL = Counter(
    "stt_errors_total",
    "Total STT processing errors",
)
# Shared pipeline metric — incremented by whichever worker dead-letters (§7)
PIPELINE_JOBS_FAILED = Counter(
    "pipeline_jobs_failed_total",
    "Pipeline jobs that were dead-lettered",
    ["stage"],
)


# ---------------------------------------------------------------------------
# S3 helpers (§4)
# ---------------------------------------------------------------------------
def _make_s3_client():
    kwargs = {
        "region_name": S3_REGION,
    }
    if S3_ENDPOINT_URL:
        # MinIO (dev): path-style addressing required
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


def s3_get_object(s3, key: str) -> bytes:
    """Download an S3 object and return raw bytes."""
    resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return resp["Body"].read()


def s3_put_object(s3, key: str, body: bytes, content_type: str = "text/plain") -> None:
    """Upload bytes to S3 under the given key (idempotent — deterministic key)."""
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=content_type)


# ---------------------------------------------------------------------------
# Redis job-state helpers (§5)
# ---------------------------------------------------------------------------
def _make_redis_client():
    return redis_client.from_url(REDIS_URL, decode_responses=True)


def set_job_state(r, job_id: str, update: dict) -> None:
    """Merge `update` into the current job state JSON and re-SET with TTL."""
    key = f"job:{job_id}"
    raw = r.get(key)
    state = json.loads(raw) if raw else {}
    state.update(update)
    state["updated_at"] = datetime.now(UTC).isoformat()
    r.set(key, json.dumps(state), ex=JOB_STATE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# STT backends
# ---------------------------------------------------------------------------
def _transcribe_stub(audio_bytes: bytes) -> tuple[str, str, float]:
    """
    Stub backend — returns a placeholder transcript.
    duration_s is estimated from byte size at 16 kHz / 16-bit mono (32 kB/s).
    Suitable for wiring demos on CPU-only clusters (dev / CI).
    Returns: (transcript_text, language, duration_s)
    """
    duration_s = max(0.1, len(audio_bytes) / 32_000)
    transcript = (
        f"[stub transcript — {len(audio_bytes)} bytes of audio, "
        f"estimated {duration_s:.1f}s @ 16kHz/16-bit mono]"
    )
    return transcript, "en", round(duration_s, 2)


def _transcribe_nemo(audio_bytes: bytes) -> tuple[str, str, float]:
    """
    NeMo / Parakeet backend — GPU only.

    IMPORTANT: nemo_toolkit and torch are NOT listed in pyproject.toml.
    They are lazy-imported here so the stub path (and tests) need no ML deps.

    The prod GPU image (Dockerfile build stage 'nemo') bakes:
      - nemo_toolkit[asr]==2.0.*
      - torch==2.2.*+cu121
      - the Parakeet RNNT-0.6B model weights at /model-cache/parakeet-rnnt-0.6b
    and sets STT_BACKEND=nemo.  This code path must NOT run on CPU-only nodes.

    Returns: (transcript_text, language, duration_s)
    """
    import tempfile

    import torch  # noqa: F401  # lazy import — fails fast if nemo not installed
    from nemo.collections.asr.models import EncDecRNNTBPEModel

    model_path = os.getenv("NEMO_MODEL_PATH", "/model-cache/parakeet-rnnt-0.6b")
    model = EncDecRNNTBPEModel.restore_from(model_path)
    model.eval()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        transcripts = model.transcribe([tmp_path])
        transcript_text = transcripts[0] if transcripts else ""
    finally:
        import os as _os

        _os.unlink(tmp_path)

    # NeMo Parakeet returns English transcripts; duration from audio length heuristic
    duration_s = max(0.1, len(audio_bytes) / 32_000)
    return transcript_text, "en", round(duration_s, 2)


def transcribe(audio_bytes: bytes) -> tuple[str, str, float]:
    """Dispatch to the configured STT backend."""
    if STT_BACKEND == "nemo":
        return _transcribe_nemo(audio_bytes)
    # Default: stub
    return _transcribe_stub(audio_bytes)


# ---------------------------------------------------------------------------
# Metrics HTTP server (separate thread, keeps deps minimal — no FastAPI)
# ---------------------------------------------------------------------------
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/healthz", "/readyz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress default access log; structlog handles it


def start_metrics_server():
    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("metrics_server_started", port=METRICS_PORT)


# ---------------------------------------------------------------------------
# Kafka helpers
# ---------------------------------------------------------------------------
def _kafka_config(extra: dict | None = None) -> dict:
    cfg = {
        "bootstrap.servers": BOOTSTRAP_SERVERS,
        "client.id": f"stt-worker-{socket.gethostname()}",
    }
    if SASL_USERNAME:
        cfg.update(
            {
                "security.protocol": SECURITY_PROTOCOL,
                "sasl.mechanism": "SCRAM-SHA-512",
                "sasl.username": SASL_USERNAME,
                "sasl.password": SASL_PASSWORD,
            }
        )
    if extra:
        cfg.update(extra)
    return cfg


def make_consumer() -> Consumer:
    cfg = _kafka_config(
        {
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,  # manual commit only after producing output event
            "max.poll.interval.ms": 300_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([TOPIC_IN])
    return consumer


def make_producer() -> Producer:
    return Producer(_kafka_config())


# ---------------------------------------------------------------------------
# Per-job processing
# ---------------------------------------------------------------------------
def process_message(
    raw_value: bytes,
    producer: Producer,
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

    # Deduplication — in-memory; survives restarts via committed Kafka offset
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
            set_job_state(
                redis,
                job_id,
                {"status": "transcribing", "stage": "stt"},
            )
        except Exception as exc:
            log.warning("redis_state_update_failed", job_id=job_id, error=str(exc))

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            start = time.perf_counter()
            try:
                # 1. Read audio from S3
                audio_bytes = s3_get_object(s3, audio_key)

                # 2. Transcribe
                transcript_text, language, duration_s = transcribe(audio_bytes)

                # 3. Write transcript to S3 (idempotent — deterministic key)
                transcript_key = f"transcripts/{job_id}"
                s3_put_object(s3, transcript_key, transcript_text.encode("utf-8"))

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


# ---------------------------------------------------------------------------
# Main consume loop
# ---------------------------------------------------------------------------
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
    s3 = _make_s3_client()
    redis = _make_redis_client()
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
