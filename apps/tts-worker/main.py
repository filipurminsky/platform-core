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

import io
import json
import os
import signal
import socket
import struct
import threading
import time
import wave
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import boto3
import redis
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

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOOTSTRAP_SERVERS = os.getenv(
    "KAFKA_BOOTSTRAP_SERVERS",
    "localhost:9092",
)
TOPIC_IN = os.getenv("KAFKA_TOPIC_IN", "audio.summaries")
TOPIC_OUT = os.getenv("KAFKA_TOPIC_OUT", "audio.results")
TOPIC_DLQ = os.getenv("KAFKA_DLQ_TOPIC", "audio.tts-dlq")
CONSUMER_GROUP = os.getenv("KAFKA_CONSUMER_GROUP", "tts-worker")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

# SASL/SCRAM — injected by Strimzi KafkaUser secret in prod; empty → plaintext for local dev
SASL_USERNAME = os.getenv("KAFKA_SASL_USERNAME", "")
SASL_PASSWORD = os.getenv("KAFKA_SASL_PASSWORD", "")
SECURITY_PROTOCOL = os.getenv(
    "KAFKA_SECURITY_PROTOCOL",
    "SASL_SSL" if SASL_USERNAME else "PLAINTEXT",
)

# S3 / MinIO
S3_BUCKET = os.getenv("S3_BUCKET", "audio-pipeline")
S3_REGION = os.getenv("S3_REGION", "eu-west-1")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")  # set to MinIO URL in dev; unset in prod

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
JOB_STATE_TTL_SECONDS = int(os.getenv("JOB_STATE_TTL_SECONDS", "604800"))

# TTS backend
TTS_BACKEND = os.getenv("TTS_BACKEND", "stub")  # stub | kokoro

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
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    service_name = os.getenv("OTEL_SERVICE_NAME", "tts-worker")
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
        return [k for k, _v in carrier or []]


class KafkaHeaderSetter(Setter):
    def set(self, carrier, key, value):
        carrier.append((key, value.encode()))


kafka_header_getter = KafkaHeaderGetter()
kafka_header_setter = KafkaHeaderSetter()

# ---------------------------------------------------------------------------
# Metrics  (contract §7 — exact names)
# ---------------------------------------------------------------------------
TTS_JOBS = Counter(
    "tts_jobs_total",
    "TTS jobs processed by outcome",
    ["status"],  # success | error | dlq
)
TTS_GENERATION_DURATION = Histogram(
    "tts_generation_duration_seconds",
    "Time spent synthesizing speech",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)
PIPELINE_COMPLETED = Counter(
    "pipeline_jobs_completed_total",
    "Total pipeline jobs completed end-to-end (terminal stage)",
)
PIPELINE_E2E_DURATION = Histogram(
    "pipeline_end_to_end_duration_seconds",
    "End-to-end pipeline duration from original job created_at to TTS completion",
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)
PIPELINE_FAILED = Counter(
    "pipeline_jobs_failed_total",
    "Pipeline jobs dead-lettered by stage",
    ["stage"],
)

# ---------------------------------------------------------------------------
# S3 client
# ---------------------------------------------------------------------------


def make_s3_client():
    kwargs: dict = {
        "region_name": S3_REGION,
    }
    if S3_ENDPOINT_URL:
        # MinIO dev — force path-style addressing
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


# ---------------------------------------------------------------------------
# Redis client
# ---------------------------------------------------------------------------


def make_redis_client():
    return redis.from_url(REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# TTS backends
# ---------------------------------------------------------------------------


def _synthesize_stub(text: str) -> bytes:
    """Generate a tiny valid WAV (0.1s of 440 Hz tone) using stdlib only.

    This is the wiring demo backend (TTS_BACKEND=stub). Used in dev and tests.
    No heavy deps required.
    """
    sample_rate = 16000
    duration_s = 0.1
    num_samples = int(sample_rate * duration_s)
    frequency = 440.0  # Hz

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        # Build PCM samples: simple square wave at 440 Hz
        # struct.pack returns bytes per sample; join them into one bytes object.
        samples = b"".join(
            struct.pack(
                "<h",
                int(32767 * 0.3 * (1 if (i * frequency // sample_rate) % 2 == 0 else -1)),
            )
            for i in range(num_samples)
        )
        wf.writeframes(samples)
    return buf.getvalue()


def _synthesize_kokoro(text: str) -> bytes:
    """Synthesize speech using the Kokoro TTS library (CPU).

    The prod CPU image bakes Kokoro model + voice packs via an extra Dockerfile
    layer / build arg. kokoro and torch are NOT in pyproject.toml so the test
    suite stays offline and the lockfile stays light.

    Import is lazy so the stub path (dev/test) never touches this code path.
    """
    # lazy import — only when TTS_BACKEND=kokoro and the prod image is used
    try:
        import kokoro  # type: ignore[import-not-found]  # prod image bakes this
    except ImportError as exc:
        raise RuntimeError(
            "kokoro is not installed. The prod CPU image bakes Kokoro model + "
            "voice packs. Use TTS_BACKEND=stub for dev/test."
        ) from exc

    # Kokoro API: pipeline returns list of (graphemes, phonemes, audio_array)
    pipeline = kokoro.KPipeline(lang_code="en-us")
    audio_chunks = []
    for _, _, audio in pipeline(text, voice="af_heart"):
        audio_chunks.append(audio)

    import numpy as np  # type: ignore[import-not-found]  # part of kokoro's deps

    audio_np = np.concatenate(audio_chunks)
    sample_rate = 24000  # Kokoro default

    # Encode to WAV bytes
    buf = io.BytesIO()
    audio_int16 = (audio_np * 32767).clip(-32768, 32767).astype("int16")
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def synthesize(text: str) -> bytes:
    """Dispatch to the configured TTS backend."""
    if TTS_BACKEND == "kokoro":
        return _synthesize_kokoro(text)
    # default: stub
    return _synthesize_stub(text)


# ---------------------------------------------------------------------------
# Metrics HTTP server
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
        pass  # suppress default access log


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
        "client.id": f"tts-worker-{socket.gethostname()}",
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
            "enable.auto.commit": False,  # manual commit only after output produced
            "max.poll.interval.ms": 300_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([TOPIC_IN])
    return consumer


def make_producer() -> Producer:
    return Producer(_kafka_config())


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


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
    producer: Producer,
    s3_client,
    redis_client,
    seen_ids: set,
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

    # Deduplication (in-memory; restarts are safe because offset is uncommitted)
    if job_id and job_id in seen_ids:
        log.info("duplicate_skipped", job_id=job_id)
        return
    if job_id:
        seen_ids.add(job_id)

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
                _redis_set_synthesizing(redis_client, job_id, summary_key)

                # Read summary JSON from S3
                log.info("reading_summary", job_id=job_id, key=summary_key, attempt=attempt)
                resp = s3_client.get_object(Bucket=S3_BUCKET, Key=summary_key)
                summary_json = resp["Body"].read().decode()
                summary_data = json.loads(summary_json)

                # Extract text to synthesize (executive_summary or action_items fallback)
                text = summary_data.get(
                    "executive_summary",
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
                _redis_set_done(redis_client, job_id, summary_key, speech_key)

                # Produce audio.results
                out_event = {
                    "job_id": job_id,
                    "speech_key": speech_key,
                    "status": "done",
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
        failed_at = _now_iso()
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
        producer.produce(
            TOPIC_DLQ,
            value=dlq_payload,
            key=job_id.encode() if job_id else None,
            headers=dlq_headers,
        )
        producer.flush(timeout=5)

        # Redis: mark failed
        _redis_set_failed(redis_client, job_id, str(last_exc))

        TTS_JOBS.labels(status="dlq").inc()
        PIPELINE_FAILED.labels(stage="tts").inc()
        log.error(
            "tts_job_dlq",
            job_id=job_id,
            error=str(last_exc),
            dlq_topic=TOPIC_DLQ,
        )


# ---------------------------------------------------------------------------
# Redis state helpers
# ---------------------------------------------------------------------------


def _redis_set_synthesizing(redis_client, job_id: str, summary_key: str) -> None:
    if not job_id:
        return
    state = {
        "status": "synthesizing",
        "stage": "tts",
        "updated_at": _now_iso(),
        "keys": {"summary": summary_key},
    }
    redis_client.set(f"job:{job_id}", json.dumps(state), ex=JOB_STATE_TTL_SECONDS)


def _redis_set_done(redis_client, job_id: str, summary_key: str, speech_key: str) -> None:
    if not job_id:
        return
    state = {
        "status": "done",
        "stage": "tts",
        "updated_at": _now_iso(),
        "keys": {"summary": summary_key, "speech": speech_key},
    }
    redis_client.set(f"job:{job_id}", json.dumps(state), ex=JOB_STATE_TTL_SECONDS)


def _redis_set_failed(redis_client, job_id: str, error_msg: str) -> None:
    if not job_id:
        return
    state = {
        "status": "failed",
        "stage": "tts",
        "updated_at": _now_iso(),
        "error": {
            "stage": "tts",
            "message": error_msg,
            "dlq_topic": TOPIC_DLQ,
        },
    }
    redis_client.set(f"job:{job_id}", json.dumps(state), ex=JOB_STATE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Main consume loop
# ---------------------------------------------------------------------------


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
                s3_client,
                redis_client,
                seen_ids,
                headers=msg.headers(),
            )

            # Manual commit — only after output or DLQ produced
            consumer.commit(message=msg, asynchronous=False)

    finally:
        log.info("tts_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("tts_worker_stopped")


if __name__ == "__main__":
    run()
